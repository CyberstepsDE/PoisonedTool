# MCP-Gemini Client

A Python client that connects to an MCP (Model Context Protocol) server and sends prompts to Google Gemini with MCP server functions available as function calling tools.

## Features

- Connects to a running MCP server via HTTP/SSE
- Sends prompts to Google Gemini API
- Makes MCP server functions available to Gemini as function calling tools
- Handles function calls automatically
- Interactive mode for conversational use

## Setup

### 1. Create Virtual Environment

```bash
python -m venv venv
```

### 2. Activate Virtual Environment

**Windows:**
```bash
venv\Scripts\activate
```

**Linux/Mac:**
```bash
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create a `.env` file in the project root with the following:

```
GEMINI_API_KEY=your_gemini_api_key_here
MCP_SERVER_HOST=localhost
MCP_SERVER_PORT=2506
MCP_SERVER_ENDPOINT=/sse
```

**Configuration:**
- `GEMINI_API_KEY`: Your Google Gemini API key (required)
- `MCP_SERVER_HOST`: Hostname or IP of your MCP server (default: localhost)
- `MCP_SERVER_PORT`: Port number of your MCP server (default: 2506)
- `MCP_SERVER_ENDPOINT`: MCP endpoint path (default: `/sse` for FastMCP)

The client will connect to your MCP server at `http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}{MCP_SERVER_ENDPOINT}`.

**Note:** FastMCP servers typically expose the MCP protocol at `/sse`. If your server uses a different endpoint (like `/mcp`), update `MCP_SERVER_ENDPOINT` accordingly.

## Usage

### Interactive Mode

Run without arguments to enter interactive mode:

```bash
python mcp_gemini_client.py
```

### Single Prompt

Provide a prompt as command line argument:

```bash
python mcp_gemini_client.py "List the files in the current directory"
```

## Example

Once configured, you can ask Gemini to use MCP server functions:

```
You: List all files in the Documents folder
Gemini: [Will call the MCP server's list_directory function and return results]
```

## Troubleshooting

- **"GEMINI_API_KEY not found"**: Make sure you've created `.env` file with your API key
- **"MCP session not initialized"**: Check that your MCP server is running and the host/port in `.env` is correct
- **Connection errors**: Verify your MCP server is running and accessible at the configured host and port
