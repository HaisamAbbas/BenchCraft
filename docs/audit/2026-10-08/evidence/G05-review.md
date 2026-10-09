# G05 independent review

The first independent review found that the JUnit XML character filter dropped legal
supplementary Unicode, including emoji, from test names and messages. The filter now accepts
XML 1.0 scalar values through U+10FFFF (while excluding invalid control and surrogate ranges),
and a regression verifies Unicode in case IDs, metric labels, and failure text.

The reviewer rechecked the final G05 changes and reported **no remaining findings**. The
review covered JUnit/SARIF result mappings, content redaction, report/TUI format routing,
SARIF structure, and the added tests. The resulting SARIF 2.1.0 documents also validate
against the official OASIS Errata 01 schema in both content modes.
