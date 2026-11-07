// Uses an existing SDK installation; this test never installs or patches packages.
// Drives the real TypeScript Streamable HTTP client (the transport most current
// MCP clients use) through initialize, tools/list, tools/call and close.
const path = require('node:path');
const { Client } = require(path.join(process.env.MCP_JS_SDK_ROOT, 'dist/cjs/client/index.js'));
const { StreamableHTTPClientTransport } = require(
  path.join(process.env.MCP_JS_SDK_ROOT, 'dist/cjs/client/streamableHttp.js'),
);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const errors = [];
  const client = new Client({ name: 'streamable-regression', version: '1' }, { capabilities: {} });
  client.onerror = error => errors.push(String(error && error.message));
  const transport = new StreamableHTTPClientTransport(new URL(process.argv[2]));
  await client.connect(transport);
  const tools = await client.listTools();
  const result = await client.callTool({ name: 'echo', arguments: { value: 'js-value' } });
  await pause(200); // Let the optional notification-stream GET settle.
  const sessionId = transport.sessionId;
  await transport.terminateSession(); // DELETE: 405 is an accepted answer.
  await client.close();
  process.stdout.write(JSON.stringify({
    tool_names: tools.tools.map(tool => tool.name),
    text: result.content.map(block => block.text).join(''),
    is_error: Boolean(result.isError),
    session_id: sessionId || null,
    protocol_version: transport.protocolVersion || null,
    errors,
  }));
}

main().catch(error => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
});
