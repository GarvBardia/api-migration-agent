import RunStartForm from "@/components/RunStartForm";
import RunList from "@/components/RunList";

export default function HomePage() {
  return (
    <div className="flex flex-col gap-12">
      <section>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          New run
        </p>
        <h1 className="mb-4 text-2xl font-semibold tracking-tight text-balance">
          Start a migration run
        </h1>
        <RunStartForm />
      </section>
      <section>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          History
        </p>
        <h2 className="mb-4 text-xl font-semibold tracking-tight">Runs</h2>
        <RunList />
      </section>
    </div>
  );
}
