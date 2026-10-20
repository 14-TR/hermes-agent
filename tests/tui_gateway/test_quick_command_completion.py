"""The profile's ``quick_commands`` reach typed ``complete.slash`` completions and rank first.

They are dispatchable by name (``command.dispatch``) and listed in ``commands.catalog``, so a user
typing ``/wa`` must see their own ``/watch`` ahead of built-ins sharing the prefix.
"""

from tui_gateway import server


def _texts(resp):
    return [item["text"] for item in resp["result"]["items"]]


def test_quick_commands_complete_first_and_stay_profile_scoped(tmp_path):
    homes = {"alpha": tmp_path / "profile-a", "beta": tmp_path / "profile-b"}
    homes["alpha"].mkdir()
    homes["beta"].mkdir()
    (homes["alpha"] / "config.yaml").write_text(
        "quick_commands:\n"
        "  watch:\n    type: exec\n    command: echo watching\n"
        "  pulse:\n    type: exec\n    command: echo pulse\n    description: Board pulse\n"
        "  help:\n    type: exec\n    command: echo shadow\n", encoding="utf-8")
    (homes["beta"] / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    try:
        for label, home in homes.items():
            server._sessions[f"qc-{label}"] = {"session_key": f"qc-{label}", "profile_home": str(home), "agent": None}

        def complete(label, text):
            return server.handle_request({"id": "t", "method": "complete.slash",
                                          "params": {"session_id": f"qc-{label}", "text": text}})

        for prefix, expected in (("/w", "watch"), ("/wa", "watch"), ("/p", "pulse")):
            texts = _texts(complete("alpha", prefix))
            assert texts and texts[0].strip().lstrip("/") == expected, (prefix, texts)
            assert expected not in [t.strip().lstrip("/") for t in _texts(complete("beta", prefix))]
        # A quick command never duplicates a registry command of the same name.
        assert [t.strip().lstrip("/") for t in _texts(complete("alpha", "/help"))].count("help") == 1

        catalog = server.handle_request({"id": "c", "method": "commands.catalog", "params": {"session_id": "qc-alpha"}})
        pairs = dict(catalog["result"]["pairs"])
        assert pairs["/pulse"] == "Board pulse"
        assert pairs["/watch"] == "exec: echo watching"
    finally:
        for label in homes:
            server._sessions.pop(f"qc-{label}", None)
