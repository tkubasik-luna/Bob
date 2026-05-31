# Chat Streaming Events (SSE)

Streaming events let you render chat responses incrementally over Server-Sent Events (SSE). When you call `POST /api/v1/chat` with `stream: true`, the server emits a series of named events you can consume. Events arrive in order and may include multiple deltas (reasoning + message content), tool-call boundaries and payloads, and any errors encountered.

The stream always **begins** with `chat.start` and **concludes** with `chat.end`, which contains the aggregated result equivalent to a non-streaming response.

## Event types

- `chat.start`
- `model_load.start`
- `model_load.progress`
- `model_load.end`
- `prompt_processing.start`
- `prompt_processing.progress`
- `prompt_processing.end`
- `reasoning.start`
- `reasoning.delta`
- `reasoning.end`
- `tool_call.start`
- `tool_call.arguments`
- `tool_call.success`
- `tool_call.failure`
- `message.start`
- `message.delta`
- `message.end`
- `error`
- `chat.end`

## Raw wire format

```
event: <event type>
data: <JSON event data>
```

---

## `chat.start`

Emitted at the start of a chat response stream.

| Field | Type | Description |
|-------|------|-------------|
| `model_instance_id` | string | Unique identifier for the loaded model instance that will generate the response. |
| `type` | `"chat.start"` | Always `chat.start`. |

```json
{
  "type": "chat.start",
  "model_instance_id": "openai/gpt-oss-20b"
}
```

## `model_load.start`

Signals the start of a model being loaded to fulfill the chat request. Not emitted if the requested model is already loaded.

| Field | Type | Description |
|-------|------|-------------|
| `model_instance_id` | string | Unique identifier for the model instance being loaded. |
| `type` | `"model_load.start"` | Always `model_load.start`. |

```json
{
  "type": "model_load.start",
  "model_instance_id": "openai/gpt-oss-20b"
}
```

## `model_load.progress`

Progress of the model load.

| Field | Type | Description |
|-------|------|-------------|
| `model_instance_id` | string | Unique identifier for the model instance being loaded. |
| `progress` | number | Progress as a float between `0` and `1`. |
| `type` | `"model_load.progress"` | Always `model_load.progress`. |

```json
{
  "type": "model_load.progress",
  "model_instance_id": "openai/gpt-oss-20b",
  "progress": 0.65
}
```

## `model_load.end`

Signals a successfully completed model load.

| Field | Type | Description |
|-------|------|-------------|
| `model_instance_id` | string | Unique identifier for the model instance that was loaded. |
| `load_time_seconds` | number | Time taken to load the model in seconds. |
| `type` | `"model_load.end"` | Always `model_load.end`. |

```json
{
  "type": "model_load.end",
  "model_instance_id": "openai/gpt-oss-20b",
  "load_time_seconds": 12.34
}
```

## `prompt_processing.start`

Signals the start of the model processing a prompt.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"prompt_processing.start"` | Always `prompt_processing.start`. |

```json
{
  "type": "prompt_processing.start"
}
```

## `prompt_processing.progress`

Progress of the model processing a prompt.

| Field | Type | Description |
|-------|------|-------------|
| `progress` | number | Progress as a float between `0` and `1`. |
| `type` | `"prompt_processing.progress"` | Always `prompt_processing.progress`. |

```json
{
  "type": "prompt_processing.progress",
  "progress": 0.5
}
```

## `prompt_processing.end`

Signals the end of the model processing a prompt.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"prompt_processing.end"` | Always `prompt_processing.end`. |

```json
{
  "type": "prompt_processing.end"
}
```

## `reasoning.start`

Signals the model is starting to stream reasoning content.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"reasoning.start"` | Always `reasoning.start`. |

```json
{
  "type": "reasoning.start"
}
```

## `reasoning.delta`

A chunk of reasoning content. Multiple deltas may arrive.

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Reasoning text fragment. |
| `type` | `"reasoning.delta"` | Always `reasoning.delta`. |

```json
{
  "type": "reasoning.delta",
  "content": "Need to"
}
```

## `reasoning.end`

Signals the end of the reasoning stream.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"reasoning.end"` | Always `reasoning.end`. |

```json
{
  "type": "reasoning.end"
}
```

## `tool_call.start`

Emitted when the model starts a tool call.

| Field | Type | Description |
|-------|------|-------------|
| `tool` | string | Name of the tool being called. |
| `provider_info` | object | Information about the tool provider. Discriminated union on provider type (see below). |
| `type` | `"tool_call.start"` | Always `tool_call.start`. |

**`provider_info` — plugin provider:**

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"plugin"` | Provider type. |
| `plugin_id` | string | Identifier of the plugin. |

**`provider_info` — ephemeral MCP provider:**

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"ephemeral_mcp"` | Provider type. |
| `server_label` | string | Label of the MCP server. |

```json
{
  "type": "tool_call.start",
  "tool": "model_search",
  "provider_info": {
    "type": "ephemeral_mcp",
    "server_label": "huggingface"
  }
}
```

## `tool_call.arguments`

Arguments streamed for the current tool call.

| Field | Type | Description |
|-------|------|-------------|
| `tool` | string | Name of the tool being called. |
| `arguments` | object | Arguments passed to the tool. Keys/values depend on the tool definition. |
| `provider_info` | object | Information about the tool provider (same discriminated union as `tool_call.start`). |
| `type` | `"tool_call.arguments"` | Always `tool_call.arguments`. |

```json
{
  "type": "tool_call.arguments",
  "tool": "model_search",
  "arguments": {
    "sort": "trendingScore",
    "limit": 1
  },
  "provider_info": {
    "type": "ephemeral_mcp",
    "server_label": "huggingface"
  }
}
```

## `tool_call.success`

Result of the tool call, along with the arguments used.

| Field | Type | Description |
|-------|------|-------------|
| `tool` | string | Name of the tool that was called. |
| `arguments` | object | Arguments that were passed to the tool. |
| `output` | string | Raw tool output string. |
| `provider_info` | object | Information about the tool provider (same discriminated union as `tool_call.start`). |
| `type` | `"tool_call.success"` | Always `tool_call.success`. |

```json
{
  "type": "tool_call.success",
  "tool": "model_search",
  "arguments": {
    "sort": "trendingScore",
    "limit": 1
  },
  "output": "[{\"type\":\"text\",\"text\":\"Showing first 1 models...\"}]",
  "provider_info": {
    "type": "ephemeral_mcp",
    "server_label": "huggingface"
  }
}
```

## `tool_call.failure`

Indicates that the tool call failed.

| Field | Type | Description |
|-------|------|-------------|
| `reason` | string | Reason for the tool call failure. |
| `metadata` | object | Metadata about the invalid tool call (see below). |
| `type` | `"tool_call.failure"` | Always `tool_call.failure`. |

**`metadata`:**

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"invalid_name"` \| `"invalid_arguments"` | Type of error that occurred. |
| `tool_name` | string | Name of the tool that was attempted. |
| `arguments` | object (optional) | Arguments passed (only for `invalid_arguments` errors). |
| `provider_info` | object (optional) | Tool provider info (only for `invalid_arguments` errors). `type` is `"plugin"` \| `"ephemeral_mcp"`, with `plugin_id` (plugin) or `server_label` (MCP). |

```json
{
  "type": "tool_call.failure",
  "reason": "Cannot find tool with name open_browser.",
  "metadata": {
    "type": "invalid_name",
    "tool_name": "open_browser"
  }
}
```

## `message.start`

Signals the model is about to stream a message.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"message.start"` | Always `message.start`. |

```json
{
  "type": "message.start"
}
```

## `message.delta`

A chunk of message content. Multiple deltas may arrive.

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | Message text fragment. |
| `type` | `"message.delta"` | Always `message.delta`. |

```json
{
  "type": "message.delta",
  "content": "The current"
}
```

## `message.end`

Signals the end of the message stream.

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"message.end"` | Always `message.end`. |

```json
{
  "type": "message.end"
}
```

## `error`

An error occurred during streaming. The final payload is still sent in `chat.end` with whatever was generated.

| Field | Type | Description |
|-------|------|-------------|
| `error` | object | Error information (see below). |
| `type` | `"error"` | Always `error`. |

**`error`:**

| Field | Type | Description |
|-------|------|-------------|
| `type` | enum | High-level error type: `invalid_request` \| `unknown` \| `mcp_connection_error` \| `plugin_connection_error` \| `not_implemented` \| `model_not_found` \| `job_not_found` \| `internal_error`. |
| `message` | string | Human-readable error message. |
| `code` | string (optional) | More detailed error code (e.g. validation issue code). |
| `param` | string (optional) | Parameter associated with the error, if applicable. |

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request",
    "message": "\"model\" is required",
    "code": "missing_required_parameter",
    "param": "model"
  }
}
```

## `chat.end`

Final event containing the full aggregated response, equivalent to the non-streaming `POST /api/v1/chat` response body.

| Field | Type | Description |
|-------|------|-------------|
| `result` | object | Final response with `model_instance_id`, `output`, `stats`, and optional `response_id`. |
| `type` | `"chat.end"` | Always `chat.end`. |

```json
{
  "type": "chat.end",
  "result": {
    "model_instance_id": "openai/gpt-oss-20b",
    "output": [
      { "type": "reasoning", "content": "Need to call function." },
      {
        "type": "tool_call",
        "tool": "model_search",
        "arguments": { "sort": "trendingScore", "limit": 1 },
        "output": "[{\"type\":\"text\",\"text\":\"Showing first 1 models...\"}]",
        "provider_info": { "type": "ephemeral_mcp", "server_label": "huggingface" }
      },
      { "type": "message", "content": "The current top-trending model is..." }
    ],
    "stats": {
      "input_tokens": 329,
      "total_output_tokens": 268,
      "reasoning_output_tokens": 5,
      "tokens_per_second": 43.73,
      "time_to_first_token_seconds": 0.781
    },
    "response_id": "resp_02b2017dbc06c12bfc353a2ed6c2b802f8cc682884bb5716"
  }
}
```
