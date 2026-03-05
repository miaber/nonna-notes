import { useState } from "react";

const CYCLE = { have: "out", out: "have" };
const STATUS_LABEL = { have: "✓", out: "✗" };
const STATUS_TITLE = { have: "Have it — click to mark as out", out: "Don't have it — click to mark as in stock" };
const ORDER = { have: 0, out: 1 };

export default function PantryPanel({ pantry, onUpdate, onDelete }) {
  const [draft, setDraft] = useState("");

  if (!pantry) return null;

  const sorted = [...pantry].sort((a, b) => (ORDER[a.status] ?? 0) - (ORDER[b.status] ?? 0));

  const handleAdd = () => {
    const name = draft.trim();
    if (!name) return;
    onUpdate([{ name, status: "have" }]);
    setDraft("");
  };

  const handleCycle = (item) => {
    const next = CYCLE[item.status] ?? "have";
    onUpdate([{ name: item.name, status: next }]);
  };

  return (
    <div className="pantry-panel">
      <h2 className="steps-heading">Pantry</h2>

      {sorted.length > 0 && (
        <ul className="pantry-list">
          {sorted.map((item, i) => (
            <li key={i} className={`pantry-item pantry-${item.status}`}>
              <button
                className="pantry-status-btn"
                onClick={() => handleCycle(item)}
                title={STATUS_TITLE[item.status]}
              >
                {STATUS_LABEL[item.status] ?? "✓"}
              </button>
              <span className="pantry-name">{item.name}</span>
              <button
                className="pantry-delete-btn"
                onClick={() => onDelete(item.name)}
                title="Remove from pantry"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="pantry-add">
        <input
          className="pantry-add-input"
          placeholder="Add ingredient…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
        />
        <button
          className="pantry-add-btn"
          onClick={handleAdd}
          disabled={!draft.trim()}
        >
          +
        </button>
      </div>
    </div>
  );
}
