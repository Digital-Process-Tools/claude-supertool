


























































from __future__ import annotations










NOT_AUTHENTICATED_MARKERS = (
    "not logged in",      
    "http 401",           
    "401 unauthorized",   
    "unauthorized",       
    "bad credentials",    
    "bad token",
    "token expired",
)











GITLAB_MARKERS = (
    "unauthenticated",    
    "authenticate",       
)


def says_not_authenticated(stderr: str, extra: tuple[str, ...] = ()) -> bool:











    low = (stderr or "").lower()
    markers = NOT_AUTHENTICATED_MARKERS + tuple(extra)
    return any(marker in low for marker in markers)
