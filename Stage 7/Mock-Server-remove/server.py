"""
Simple MCP server that removes a file from a target directory.
"""

from fastmcp import FastMCP
from pathlib import Path
from typing import Dict, Any

# Target directory for file operations
TARGET_DIR = r"..\dir"

# Create the MCP app
mcp = FastMCP("Remove File Server")


@mcp.tool()
def remove_file(filename: str) -> Dict[str, Any]:
    """
    Remove a file from the target directory by name.

    Args:
        filename: The name of the file to delete (no path components; must be a single filename).

    Returns a dictionary containing:
    - success: Whether the file was deleted
    - path: The full path that was attempted
    - message: Success or error message
    """
    try:
        target_dir = Path(TARGET_DIR)

        if not target_dir.exists():
            return {
                "success": False,
                "path": str(target_dir / filename),
                "message": f"Directory does not exist: {TARGET_DIR}",
            }

        if not target_dir.is_dir():
            return {
                "success": False,
                "path": str(target_dir / filename),
                "message": f"Path is not a directory: {TARGET_DIR}",
            }

        # Restrict to filename only (no path traversal)
        if Path(filename).name != filename or ".." in filename or "/" in filename or "\\" in filename:
            return {
                "success": False,
                "path": str(target_dir / filename),
                "message": "Invalid filename: path components not allowed",
            }

        file_path = target_dir / filename

        if not file_path.exists():
            return {
                "success": False,
                "path": str(file_path),
                "message": f"File does not exist: {filename}",
            }

        if file_path.is_dir():
            return {
                "success": False,
                "path": str(file_path),
                "message": f"Cannot remove directory (use a different tool): {filename}",
            }

        file_path.unlink()
        return {
            "success": True,
            "path": str(file_path),
            "message": f"Deleted: {filename}",
        }
    except PermissionError as e:
        return {
            "success": False,
            "path": str(Path(TARGET_DIR) / filename),
            "message": f"Permission denied: {e}",
        }
    except Exception as e:
        return {
            "success": False,
            "path": str(Path(TARGET_DIR) / filename),
            "message": str(e),
        }


if __name__ == "__main__":
    mcp.run(transport="sse", host="127.0.0.1", port=3101)
