# Frontend shared layer

## Responsibility and ownership

- **Owner:** frontend maintainers; simulation domain maintainers review changes that alter displayed domain terminology.
- **Responsibilities:** stable cross-feature API adapters, presentation helpers, and reusable page chrome or UI components.
- **Non-responsibilities:** electrical calculations, control or safety decisions, feature workflows, and duplicate API contract models.

## Public modules

- `api/`: generated or shared API access and contract-facing helpers.
- `lib/region-presentation.ts`: the single Web presentation mapping from stable internal region codes to user-facing names.
- `lib/execution-display-text.ts`: formats source/boundary prose at render sites. It removes repeated comparisons with field measurements and shortens observation/execution labels while retaining numerical assumptions and technical conclusions. Stored metadata, API values, replay inputs and exports retain their original text; never apply this helper to identifiers or entire result objects. Platform/page names and service connection labels retain their existing simulation terminology.
- `ui/platform-chrome.tsx`: common page header and navigation.

  Only `/patent2` and `/single-microgrid` are exposed in navigation. The `/demo-lab`, `/cluster-coordination`, and `/communication-fault` page entry points redirect to `/patent2` on the server without mounting their previous components or forwarding query parameters. Their reusable feature components and backend capabilities remain available to the two active pages.
- `ui/saved-record-selector.tsx`: shared saved-example selector and refresh control for the single-grid and patent2 pages; pages own record loading and calculation state.

User-facing region names use the shared presentation mapping instead of maintaining duplicate code-to-name lists.

## Dependency and compatibility rules

- `shared` must not import from `features`, page entry points, or application composition code.
- Internal identifiers, API payloads, equipment IDs, and bus IDs continue to use raw region codes.
- Unknown region codes are displayed unchanged so newly introduced backend values are not mislabeled or hidden.
- The listed regions are parameterized synthetic demonstration models; their display names must not imply a verified field network.

## Verification

- Unit tests for shared presentation helpers live in `tests/`.
- Consumers are covered by TypeScript checks, frontend linting, and the production build.
