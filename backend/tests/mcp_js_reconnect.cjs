// Uses an existing SDK installation; this test never installs or patches packages.
const path = require('node:path');
const { Client } = require(path.join(process.env.MCP_JS_SDK_ROOT, 'dist/cjs/client/index.js'));
const { SSEClientTransport } = require(path.join(process.env.MCP_JS_SDK_ROOT, 'dist/cjs/client/sse.js'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const errors = [];
  const oldClient = new Client({ name: 'reconnect-regression', version: '1' }, { capabilities: {} });
  const freshClient = new Client({ name: 'fresh-regression', version: '1' }, { capabilities: {} });
  oldClient.onerror = () => errors.push(Date.now());
  const waitErrors = async count => {
    const deadline = Date.now() + 15000;
    while (errors.length < count) {
      if (Date.now() > deadline) throw new Error('Missing expected expiry');
      await pause(10);
    }
  };
  try {
    await oldClient.connect(new SSEClientTransport(new URL(process.argv[2])));
    await oldClient.listTools();
    await waitErrors(2); // Initialized idle EOF, then uninitialized replacement EOF.
    await freshClient.connect(new SSEClientTransport(new URL(process.argv[2])));
    const fresh = await freshClient.listTools();
    await freshClient.close();
    const retry = Number(process.argv[3]);
    if (retry === 300000) {
      await pause(4200); // Past the old default retry, without a five-minute unit test.
      if (errors.length !== 2) throw new Error('Default backoff was not honored');
    } else {
      await waitErrors(3);
      if (errors[2] - errors[1] < retry) throw new Error('Retry happened too early');
    }
    await oldClient.close();
    if (retry !== 300000) await pause(retry + 500); // Closing cancels the next retry.
    process.stdout.write(JSON.stringify({
      fresh_tools_ok: Array.isArray(fresh.tools),
      expiry_count: errors.length,
      retry_milliseconds: retry,
    }));
  } finally {
    await oldClient.close();
    await freshClient.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
