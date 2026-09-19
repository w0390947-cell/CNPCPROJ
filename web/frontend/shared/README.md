# Frontend shared layer

## Responsibility and ownership

- **Owner:** frontend maintainers; simulation domain maintainers review changes that alter displayed domain terminology.
- **Responsibilities:** stable cross-feature API adapters, presentation helpers, and reusable page chrome or UI components.
- **Non-responsibilities:** electrical calculations, control or safety decisions, feature workflows, and duplicate API contract models.

## Public modules

- `api/`: generated or shared API access and contract-facing helpers.
- `lib/region-presentation.ts`: the single Web presentation mapping from stable internal region codes to user-facing names.
- `ui/platform-chrome.tsx`: common page header and navigation.

The user-facing region reference table belongs to the `case-information` feature. It consumes the shared presentation mapping instead of maintaining another code-to-name list.

## Dependency and compatibility rules

- `shared` must not import from `features`, page entry points, or application composition code.
- Internal identifiers, API payloads, equipment IDs, and bus IDs continue to use raw region codes.
- Unknown region codes are displayed unchanged so newly introduced backend values are not mislabeled or hidden.
- The listed regions are parameterized synthetic demonstration models; their display names must not imply a verified field network.

## Verification

- Unit tests for shared presentation helpers live in `tests/`.
- Consumers are covered by TypeScript checks, frontend linting, and the production build.
