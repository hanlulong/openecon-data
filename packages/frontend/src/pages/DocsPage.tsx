import { useEffect } from 'react'

export function DocsPage() {
  useEffect(() => {
    document.title = 'Documentation | openecon.ai'
  }, [])

  return (
    <div className="min-h-screen bg-gray-50 py-8 sm:py-12 lg:py-16">
      <div className="mx-auto max-w-4xl px-4 sm:px-6">
        <h1 className="text-2xl font-semibold text-gray-900 sm:text-3xl">Documentation</h1>
        <p className="mt-3 text-sm text-gray-600 leading-relaxed sm:mt-4 sm:text-base">
          openecon.ai combines large language models with curated macroeconomic data connectors. The
          marketing site demonstrates the assistant experience: type a natural-language question,
          review the structured response, and export the underlying dataset. This page captures the
          basics. For SDKs, authentication, and deployment guides, visit{' '}
          <a
            className="text-indigo-600 underline"
            href="https://docs.openecon.ai"
            target="_blank"
            rel="noreferrer"
          >
            docs.openecon.ai
          </a>
          .
        </p>

        <div className="mt-8 space-y-8 text-gray-700 sm:mt-10 sm:space-y-10 lg:mt-12">
          <section>
            <h2 className="text-lg font-semibold text-gray-900 sm:text-xl">Key capabilities</h2>
            <ul className="mt-2 list-disc space-y-2 pl-5 text-sm text-gray-600 sm:mt-3 sm:text-base">
              <li>
                <span className="font-medium text-gray-800">Automated integrations:</span> FRED, World Bank, UN
                Comtrade, BIS, IMF, and more are routed automatically based on the assistant’s interpretation.
              </li>
              <li>
                <span className="font-medium text-gray-800">LLM intent parsing:</span> we prompt the model to return a JSON
                structure describing the provider, parameters, and recommended visualization.
              </li>
              <li>
                <span className="font-medium text-gray-800">Traceable responses:</span> every assistant answer exposes
                metadata (source, frequency, series ID) and a copyable API URL.
              </li>
              <li>
                <span className="font-medium text-gray-800">Exports:</span> download the normalized result set as CSV or JSON,
                or copy the prefilled curl command for reproducibility.
              </li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-gray-900 sm:text-xl">Hero demo explained</h2>
            <p className="mt-2 text-sm text-gray-600 leading-relaxed sm:mt-3 sm:text-base">
              The landing-page “Try a semantic query” card is wired to the production assistant. The default prompt
              fetches the Canadian unemployment rate (Statistics Canada table 14-10-0287-01). The response block shows:
            </p>
            <ul className="mt-2 list-disc space-y-2 pl-5 text-sm text-gray-600 sm:mt-3 sm:text-base">
              <li>LLM intent output (provider, frequency, date range).</li>
              <li>The exact REST request that will be sent to the backend.</li>
              <li>A summarized assistant narrative plus the actual annual rates that back the chart.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-gray-900 sm:text-xl">Quick start</h2>
            <ol className="mt-2 list-decimal space-y-2 pl-5 text-sm text-gray-600 sm:mt-3 sm:text-base">
              <li>Click <em>Launch demo</em> and sign in using your OpenRouter API key (prompted on first query).</li>
              <li>Submit a natural-language question such as “Canada unemployment rate SA, monthly, last 10 years”.</li>
              <li>
                Inspect the assistant’s response, copy the source URL, or download the result as CSV/JSON for your
                notebook.
              </li>
            </ol>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-gray-900 sm:text-xl">Need more?</h2>
            <p className="mt-2 text-sm text-gray-600 sm:text-base">
              Our full documentation covers SDK usage (Python/TypeScript), infrastructure deployment, and enterprise
              authentication. Reach us at{' '}
              <a className="text-indigo-600 underline" href="mailto:hanlulong@gmail.com">
                hanlulong@gmail.com
              </a>{' '}
              for onboarding assistance.
            </p>
          </section>
        </div>
      </div>
    </div>
  )
}
