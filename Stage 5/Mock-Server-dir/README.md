# Directory List MCP Server

A simple Model Context Protocol (MCP) server built with Python that provides access to list the contents of a specific directory.

## Features

- Lists files and directories in `C:\Users\urisa\Documents\roman\MCP\Mock-Server\dir`
- Provides file metadata including name, type, size, and modification timestamp

## Installation

### Quick Setup
Run the setup script to create a virtual environment and install dependencies:
```
setup.bat
```

### Manual Setup
1. Create a virtual environment:
```
python -m venv venv
```

2. Activate the virtual environment:
```
venv\Scripts\activate.bat
```

3. Install dependencies:
```
pip install -r requirements.txt
```

## Usage

### Quick Run
```
run.bat
```

### Manual Run
1. Activate the virtual environment:
```
venv\Scripts\activate.bat
```

2. Run the server:
```
python server.py
```

The server communicates via stdio and can be connected to MCP clients like Claude Desktop or Cursor.

## MCP Tool

The server exposes a single tool:

- **list_directory**: Lists all files and directories in the target directory, including metadata (name, type, size, modification timestamp)

## Configuration

To change the target directory, modify the `TARGET_DIR` constant in `server.py`.

## Example Output

The `list_directory` tool returns a JSON object with:
- `path`: The directory path
- `items`: Array of file/directory objects with metadata
- `count`: Total number of items
