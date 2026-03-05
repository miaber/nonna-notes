import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google.genai import errors as genai_errors
from parser import parse_recipe

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_URL = os.getenv("RECIPE_AGENT_URL", "http://localhost:8001")

# A2A-compliant Agent Card (spec: https://a2a-protocol.org/latest/specification/)
AGENT_CARD = {
    "schemaVersion": "1.0",
    "humanReadableId": "mise/recipe-agent",
    "agentVersion": "1.0.0",
    "name": "Mise Recipe Agent",
    "description": "Parses recipe URLs or generates structured recipes from a name or description. Part of the Mise cooking assistant.",
    "url": _BASE_URL,
    "provider": {"name": "Mise"},
    "capabilities": {
        "a2aVersion": "1.0",
        "streaming": False,
        "supportedMessageParts": ["text", "data"],
    },
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["application/json"],
    "authSchemes": [{"type": "none"}],
    "skills": [
        {
            "id": "parse_recipe",
            "name": "Parse or Generate Recipe",
            "description": (
                "Given a recipe URL or dish name/description, returns a structured recipe object. "
                "URL input fetches and parses the page. Text input generates a recipe via Gemini."
            ),
            "tags": ["recipe", "cooking", "food"],
            "examples": [
                "https://www.allrecipes.com/recipe/12345/carbonara/",
                "spaghetti carbonara for 2",
                "my mum's lasagne",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Recipe URL or dish name/description"},
                    "persona": {
                        "type": "string",
                        "enum": ["gordon", "nonna"],
                        "description": "Chef persona — affects tone of generated recipes",
                    },
                },
                "required": ["input"],
            },
        }
    ],
}


class ParseRequest(BaseModel):
    input: str
    persona: str = "gordon"


@app.get("/.well-known/agent.json")
async def agent_card():
    return JSONResponse(content=AGENT_CARD)


@app.post("/parse")
async def parse(req: ParseRequest):
    if not req.input.strip():
        raise HTTPException(status_code=400, detail="input is required")
    try:
        recipe, source = await parse_recipe(req.input.strip(), req.persona)
        return {"recipe": recipe.model_dump(), "source": source}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        import traceback
        print(f"[recipe-agent] parse error: {e}\n{traceback.format_exc()}", flush=True)
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")
    except genai_errors.ClientError as e:
        if getattr(e, "code", None) == 429:
            raise HTTPException(
                status_code=503,
                detail="API rate limit exceeded. Please try again in a minute.",
            )
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "recipe-agent"}
