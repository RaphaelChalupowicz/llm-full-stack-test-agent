import os


def select_strategy(file_relative: str) -> str:
    """
    Selects a testing strategy based on the file location and name.
    Args:
        file_relative (str): The relative path to the file.
    Returns:
        str: The selected testing strategy.

    """

    path = file_relative.replace("\\", "/")
    name = os.path.basename(path)
    name_lower = name.lower()

    if "/redux/thunks/" in path:
        return "redux_thunk"
    if "/redux/slices/" in path:
        return "redux_slice"
    if "/services/" in path:
        return "service"
    if "/pages/" in path:
        return "page"
    if "/components/" in path:
        return "component"
    if "/hooks/" in path or name_lower.startswith("use"):
        return "util"
    if "/contexts/" in path:
        return "component"
    if "/utils/" in path or "/helpers/" in path or "/lib/" in path:
        return "util"

    return "util"