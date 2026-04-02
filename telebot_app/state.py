import threading
from typing import Any, Dict

user_states: Dict[int, Dict[str, Any]] = {}


def get_state(user_id: int) -> Dict[str, Any]:
    return user_states.setdefault(int(user_id), {})


def set_state(user_id: int, data: Dict[str, Any]) -> None:
    user_states[int(user_id)] = data


def clear_state(user_id: int) -> None:
    user_states.pop(int(user_id), None)


def run_async(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
