from pydantic import BaseModel
from typing import Optional


class Ingredient(BaseModel):
    amount: str
    item: str
    prep: str = ""


class Step(BaseModel):
    id: int
    instruction: str
    timer_seconds: Optional[int] = None
    visual_checkpoint: bool = False


class RecipeSchema(BaseModel):
    name: str
    description: str
    servings: Optional[int] = 2
    total_time_minutes: Optional[int] = None
    ingredients: list[Ingredient]
    steps: list[Step]
    tips: list[str] = []
