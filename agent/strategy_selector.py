import os


# given a file path select a testing strategy based on its location and name
def select_strategy(file_relative: str) -> str:
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