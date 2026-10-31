import care.triage.engine as triage_core
from care import screen_patient as screen


def run_workflow(item_id: str) -> str:
    return screen(item_id)
