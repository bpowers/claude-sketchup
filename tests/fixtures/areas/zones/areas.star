# Two areas: b reads a, so its alternative p is fitted to whichever of a's is on, and cannot stand in today's a.

{
    "AREAS": [{"id": "a", "name": "the first area"}, {"id": "b", "name": "the second area", "reads": ["a"]}],
    "VIEWS": {
        "ax_bp": {"name": "x with p", "choose": {"a": "x", "b": "p"}},
        "ay": {"name": "y alone", "choose": {"a": "y"}},
    },
}
