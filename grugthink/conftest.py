"""Top-level pytest conftest (grugthink/ is pytest.ini's rootdir).

pytest_plugins must live in a ROOT-level conftest.py - CodeRabbit #630:
it was previously declared in tests/conftest.py, a non-top-level conftest
relative to this rootdir, which current pytest rejects at collection time
(deprecated since pytest 4.0, since removed). Moved here unchanged.
"""

# Registers src/grugthink/conftest.py's LLM API mock fixtures (mock_gemini_api,
# mock_ollama_api, mock_ollama_errors, mock_gemini_module, mock_gemini_errors,
# etc.) - pytest only auto-discovers conftest.py files in a test's own
# directory and its ancestors, and src/grugthink/conftest.py is not an
# ancestor of tests/, so it must be registered explicitly.
pytest_plugins = ["src.grugthink.conftest"]
