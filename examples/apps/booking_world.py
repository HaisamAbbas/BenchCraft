"""A tool-using booking agent with an isolated test world (15-T2 fixture).

The world is a test double held in memory: flights with seats, bookings, and an outbox.
Nothing here books, charges or emails anything real. The "agent" is scripted and
deterministic, with the mistakes real agents make, so a benchmark has something to catch:

- it books a flight named in the request (`Book flight BA117 for Ada`) or found by
  destination (`Book the London flight for Grace`), and cancels the last booking;
- it passes the flight code as written, so `ba-117` fails in the tool;
- it claims success even when the booking tool failed (sold out, unknown flight);
- asked to email the ticket, it tries `send_email`, which the world denies unless the seed
  allows email;
- a request containing "crash" fails with HTTP 500 (an application failure, for testing
  what happens to the rest of an episode).

POST /agent  {"input": str}  -> {"output", "tool_events", "world_state"}
POST /reset  seed or {}      -> the world restarts from the seed (or the default world)
GET  /health

    python booking_world.py --port 8768
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_WORLD: dict[str, Any] = {
    "flights": {
        "BA117": {"destination": "London", "seats": 2},
        "AF22": {"destination": "Paris", "seats": 0},
    },
    "bookings": [],
    "outbox": [],
    "allow_email": False,
    "next_booking": 1,
}


class World:
    def __init__(self) -> None:
        self.state = copy.deepcopy(DEFAULT_WORLD)
        self.events: list[dict[str, Any]] = []

    def _record(self, name: str, arguments: dict[str, Any], status: str, **extra: Any) -> dict:
        event = {"name": name, "arguments": arguments, "status": status, **extra}
        self.events.append(event)
        return event

    # --- tools the agent may call ---------------------------------------------------------

    def search_flights(self, destination: str) -> dict[str, Any]:
        found = [
            code
            for code, flight in self.state["flights"].items()
            if flight["destination"].lower() == destination.lower()
        ]
        return self._record("search_flights", {"destination": destination}, "ok", result=found)

    def book_flight(self, flight: str, passenger: str) -> dict[str, Any]:
        args = {"flight": flight, "passenger": passenger}
        record = self.state["flights"].get(flight)
        if record is None:
            return self._record("book_flight", args, "error", error=f"unknown flight {flight}")
        if record["seats"] <= 0:
            return self._record("book_flight", args, "error", error=f"{flight} is sold out")
        record["seats"] -= 1
        booking = {"id": f"B{self.state['next_booking']}", "flight": flight, "passenger": passenger}
        self.state["next_booking"] += 1
        self.state["bookings"].append(booking)
        return self._record("book_flight", args, "ok", result=booking["id"])

    def cancel_booking(self, booking_id: str) -> dict[str, Any]:
        args = {"booking_id": booking_id}
        for booking in self.state["bookings"]:
            if booking["id"] == booking_id:
                self.state["bookings"].remove(booking)
                self.state["flights"][booking["flight"]]["seats"] += 1
                return self._record("cancel_booking", args, "ok", result=booking_id)
        return self._record("cancel_booking", args, "error", error=f"no booking {booking_id}")

    def send_email(self, to: str, body: str) -> dict[str, Any]:
        args = {"to": to, "body": body}
        if not self.state.get("allow_email"):
            return self._record("send_email", args, "denied", error="email is not allowed here")
        self.state["outbox"].append({"to": to, "body": body})
        return self._record("send_email", args, "ok")


def agent(world: World, text: str) -> str:
    """The scripted agent: plans tool calls from the request, then answers."""
    world.events = []
    lowered = text.lower()
    if "cancel" in lowered:
        if not world.state["bookings"]:
            return "You have no bookings to cancel."
        last = world.state["bookings"][-1]["id"]
        world.cancel_booking(last)
        return f"Cancelled booking {last}."
    passenger = (re.search(r"\bfor ([A-Z][a-z]+)", text) or [None, "Guest"])[1]
    named = re.search(r"\bflight ([A-Za-z0-9-]+)", text)
    if named and named[1].lower() not in ("to", "for"):
        flight = named[1]  # as written: "ba-117" is not normalized (a realistic bug)
    else:
        destination = (re.search(r"\bthe ([A-Z][a-z]+) flight", text) or [None, ""])[1]
        found = world.search_flights(destination)["result"]
        if not found:
            return f"I found no flight to {destination or 'that destination'}."
        flight = found[0]
    world.book_flight(flight, passenger)
    reply = f"Booked {flight} for {passenger}."  # claims success whatever the tool said
    if "email" in lowered:
        world.send_email(f"{passenger.lower()}@example.com", f"Your ticket for {flight}")
        reply += " I emailed the ticket."
    return reply


class BookingServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        self.world = World()
        self.lock = threading.Lock()
        self.calls: Counter[str] = Counter()


class Handler(BaseHTTPRequestHandler):
    server: BookingServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = self._body()
        except json.JSONDecodeError:
            self._send(400, {"error": "body must be JSON"})
            return
        with self.server.lock:
            self.server.calls[self.path] += 1
            world = self.server.world
            if self.path == "/reset":
                world.state = copy.deepcopy(body or DEFAULT_WORLD)
                world.state.setdefault("bookings", [])
                world.state.setdefault("outbox", [])
                world.state.setdefault("next_booking", 1)
                self._send(200, {"status": "reset"})
            elif self.path == "/agent":
                if "crash" in str(body.get("input", "")).lower():
                    self._send(500, {"error": "the agent crashed"})  # an application failure
                    return
                reply = agent(world, str(body.get("input", "")))
                self._send(
                    200,
                    {
                        "output": reply,
                        "tool_events": world.events,
                        "world_state": copy.deepcopy(world.state),
                    },
                )
            else:
                self._send(404, {"error": "not found"})


def make_server(host: str = "127.0.0.1", port: int = 8768) -> BookingServer:
    return BookingServer((host, port))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"serving on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
