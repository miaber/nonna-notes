import io
import os
import traceback
from typing import List
from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from google.genai import errors as genai_errors
from parser import parse_recipe, parse_recipe_images

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
    "humanReadableId": "nonna-notes/recipe-agent",
    "agentVersion": "1.0.0",
    "name": "Nonna Notes Recipe Agent",
    "description": "Parses recipe URLs or generates structured recipes from a name or description. Part of the Nonna Notes cooking app.",
    "url": _BASE_URL,
    "provider": {"name": "Nonna Notes"},
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
    persona: str = "nonna"


@app.get("/")
@app.get("/health")
async def health():
    """Liveness/readiness for Cloud Run or load balancers. Use this instead of POST /parse for health checks."""
    return JSONResponse(content={"status": "ok", "service": "mise-recipe-agent"})


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
    except genai_errors.ClientError as e:
        if getattr(e, "code", None) == 429:
            raise HTTPException(status_code=503, detail="API rate limit exceeded. Please try again in a minute.")
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")
    except Exception as e:
        print(f"[recipe-agent] parse error: {e}\n{traceback.format_exc()}", flush=True)
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")


@app.post("/parse-image")
async def parse_image(
    images: List[UploadFile] = File(...),
    persona: str = Form("nonna"),
):
    image_data = []
    total_bytes = 0
    for img in images:
        if not img.content_type or not img.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"File must be an image, got {img.content_type}")
        raw = await img.read()
        total_bytes += len(raw)
        if total_bytes > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Total image size too large (max 20MB)")
        image_data.append((raw, img.content_type))
    if not image_data:
        raise HTTPException(status_code=400, detail="At least one image is required")
    try:
        recipe, source = await parse_recipe_images(image_data, persona)
        return {"recipe": recipe.model_dump(), "source": source}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except genai_errors.ClientError as e:
        if getattr(e, "code", None) == 429:
            raise HTTPException(status_code=503, detail="API rate limit exceeded. Please try again in a minute.")
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")
    except Exception as e:
        print(f"[recipe-agent] image parse error: {e}\n{traceback.format_exc()}", flush=True)
        raise HTTPException(status_code=500, detail=f"Recipe parse error: {e}")


@app.post("/convert-preview")
async def convert_preview(image: UploadFile = File(...)):
    """Convert a HEIC/HEIF image to JPEG for browser preview."""
    try:
        from PIL import Image
        from pillow_heif import register_heif_opener
        register_heif_opener()

        raw = await image.read()
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((400, 400))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        return Response(content=buf.getvalue(), media_type="image/jpeg")
    except Exception as e:
        print(f"[recipe-agent] convert-preview error: {e}", flush=True)
        raise HTTPException(status_code=422, detail=f"Could not convert image: {e}")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "recipe-agent"}
