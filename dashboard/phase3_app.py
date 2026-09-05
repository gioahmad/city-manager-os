import schedule_app

# Registers Phase 3 recurring operations, awareness and verification routes.
import operations_routines_app  # noqa: F401,E402
# Adds routine date windows and per-occurrence notes without replacing the core engine.
import operations_occurrence_controls  # noqa: F401,E402
# Adds local PostGIS-backed flood intelligence.
import flood_app  # noqa: F401,E402
# Adds the browser-based Mapping Center and web-managed GIS layers.
import map_app  # noqa: F401,E402
# Adds the next-phase web control plane: integrations, API Lab and regional event intelligence.
import integrations_app  # noqa: F401,E402

app = schedule_app.app
