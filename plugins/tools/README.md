# tools - remaining leaf utilities

| file | capability |
|---|---|
| `model_cache_refresh.py` | treats the model catalog as a cache and refreshes it from the provider API |
| `model_econ.py` | cost / speed / time-per-task model over that catalog |

Nothing here drives a workflow; callers use these directly.

    import model_cache_refresh as mcr
    mcr.refresh_models()
