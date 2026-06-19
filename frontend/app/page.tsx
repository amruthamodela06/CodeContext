import { IngestForm } from "./_components/IngestForm";

export default function Home() {
  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-gray-900">
          CodeContext
        </h1>
        <p className="mt-2 text-sm text-gray-600">
          Ingest a public GitHub repo, embed its code, and ask cited questions.
        </p>
      </header>
      <div className="mt-8">
        <IngestForm />
      </div>
    </main>
  );
}
