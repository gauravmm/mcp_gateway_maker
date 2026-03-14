"""Allow running as: python -m mcp_proxy"""

from .cli import main

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass  # Allow clean exit without stack trace
