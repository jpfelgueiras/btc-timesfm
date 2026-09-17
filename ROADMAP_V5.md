# Roadmap V5: Web Interface & Reliability Enhancement

This roadmap focuses on stabilizing and improving the user experience for the [btc-timesfm](https://jpfelgueiras.github.io/btc-timesfm/) web interface.

## Objectives

- [x] Fix chart rendering issues.
- [x] Resolve functional bugs in the Forecast Explorer filters.
- [x] Transition from a single-page view to a tabbed interface.
- [x] Implement end-to-end tests for validation of web endpoints.

## Completed Tasks

### 1. Web Frontend & UX

- [x] **Tabbed Interface Implementation:** Redesigned the dashboard layout to use a tabbed interface (Overview, Forecast Explorer, Model Metrics, Historical Analysis).
- [x] **Chart Rendering Fix:** Added `width="100%" height="auto"` to SVG elements for proper responsive rendering.
- [x] **Filter Functionality Repair:** Fixed search filter to include regime and status text in searchable content.

### 2. Testing & Validation

- [x] **Endpoint Validation:** Comprehensive test suite validates data consistency and API responses for the web service via `test_site_contract.py`.
- [x] **Regression Testing:** Added UI regression tests to ensure charts/filters remain functional in future updates.

### 3. Documentation

- [x] Updated [docs/FORECAST_API_CONTRACT.md](docs/FORECAST_API_CONTRACT.md) to reflect site structure changes.