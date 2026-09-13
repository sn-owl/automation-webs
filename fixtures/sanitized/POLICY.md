# Sanitized Fixture Policy

Sanitized fixtures preserve the board HTML structure needed by parsers while removing business-identifying data.

## Preserve

- HTML element hierarchy, table/list structure, form field names, attachment link shape, and date/title/body locations.
- Publicly safe placeholder text that keeps parser-relevant labels and spacing.
- Deterministic scenario labels such as `alpha-ready`, `beta-manual`, and `egov-developable`.

## Remove or replace

- Person names, department contacts, phone numbers, mobile numbers, email addresses, and facility street addresses.
- Internal hostnames, private IP URLs, admin URLs, query strings that identify real posts, and file paths from company machines.
- Cookies, session IDs, CSRF tokens, bearer/API tokens, and hidden credential-like values.
- Original attachment contents unless separately sanitized and covered by this policy.

## Replacement rules

- Use stable placeholders: `PERSON_001`, `DEPARTMENT_001`, `PHONE_001`, `URL_001`, `TOKEN_001`.
- Keep one-to-one replacement within a fixture so duplicate detection and parser behavior remain testable.
- Never store the replacement map beside public fixtures.
