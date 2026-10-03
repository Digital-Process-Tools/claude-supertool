


















































from __future__ import annotations








NOT_FOUND_MARKERS = (
    "could not resolve",  
    "not found",           
    "http 404",            
)




FORBIDDEN_MARKERS = (
    "forbidden",           
    "permission denied",   
    "http 403",            
)


def says_not_found(stderr: str) -> bool:






    low = (stderr or "").lower()
    return any(marker in low for marker in NOT_FOUND_MARKERS)


def says_forbidden(stderr: str) -> bool:




    low = (stderr or "").lower()
    return any(marker in low for marker in FORBIDDEN_MARKERS)
