function formatIngredient(ing) {
  if (typeof ing === "string") return ing;
  if (ing && typeof ing === "object" && "item" in ing) {
    const parts = [ing.amount, ing.item].filter(Boolean);
    if (ing.prep) parts.push(`(${ing.prep})`);
    return parts.join(" ").trim() || ing.item || "";
  }
  return String(ing);
}

export default function IngredientsPanel({ ingredients }) {
  if (!ingredients || ingredients.length === 0) return null;

  return (
    <div className="ingredients-panel">
      <h2 className="steps-heading">Ingredients</h2>
      <ul className="ingredients-list">
        {ingredients.map((ing, i) => (
          <li key={i} className="ingredient-item">
            {typeof ing === "object" && ing != null && "item" in ing ? (
              <>
                <span className="ingredient-amount">{ing.amount ?? ""}</span>
                <span className="ingredient-main">
                  <span className="ingredient-name">{ing.item ?? ""}</span>
                  {ing.prep && <span className="ingredient-prep"> {ing.prep}</span>}
                </span>
              </>
            ) : (
              formatIngredient(ing)
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
