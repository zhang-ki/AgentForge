import orbit.adapters.telemetry as telemetry_core
from orbit import sample_frame as sample


def run_workflow(item_id: str) -> str:
    return sample(item_id)
