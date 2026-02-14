"""
Simple MCP server that lists the contents of a target directory.
"""

from fastmcp import FastMCP
from pathlib import Path
from typing import Dict, Any

# Target directory to list
TARGET_DIR = r".\dir"

# Create the MCP app
mcp = FastMCP("Directory List Server")


@mcp.tool()
def list_directory() -> Dict[str, Any]:
    """
   
    Returns a dictionary containing:
    - path: The directory path
    - items: List of files and directories with metadata
    - count: Total number of items
    """
    try:
        target_path = Path(TARGET_DIR)
        
        if not target_path.exists():
            return {
                "error": f"Directory does not exist: {TARGET_DIR}",
                "path": TARGET_DIR,
                "items": [],
                "count": 0
            }
        
        if not target_path.is_dir():
            return {
                "error": f"Path is not a directory: {TARGET_DIR}",
                "path": TARGET_DIR,
                "items": [],
                "count": 0
            }
        
        items = []
        for item in target_path.iterdir():
            try:
                stat = item.stat()
                items.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": stat.st_size if item.is_file() else None,
                    "modified": stat.st_mtime,
                })
            except (OSError, PermissionError) as e:
                items.append({
                    "name": item.name,
                    "type": "unknown",
                    "error": str(e)
                })
        
        return {
            "path": str(target_path),
            "items": items,
            "count": len(items)
        }
    except Exception as e:
        return {
            "error": str(e),
            "path": TARGET_DIR,
            "items": [],
            "count": 0
        }


if __name__ == "__main__":
    mcp.run(transport="sse", host="127.0.0.1", port=2506)
