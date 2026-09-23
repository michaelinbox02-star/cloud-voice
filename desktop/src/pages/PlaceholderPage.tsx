type Props = {
  eyebrow: string;
  title: string;
  summary: string;
  missing: string[];
};

export function PlaceholderPage({ eyebrow, title, summary, missing }: Props) {
  return (
    <div className="page">
      <div className="eyebrow">{eyebrow}</div>
      <h1>{title}</h1>
      <p className="lede">{summary}</p>
      <section className="panel">
        <div className="panel-head">
          <span className="step">!</span>
          <div>
            <h2>Not built yet</h2>
            <p>This screen is deliberately empty rather than a mock. It ships when the backend below works.</p>
          </div>
        </div>
        <ul className="checklist">
          {missing.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}
