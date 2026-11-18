"""People tagging: describes a detected person with appearance tags (man/woman, hair, hat, glasses,
clothes, shoes, bag and their colours).

Model: CLIP ViT-B/32 (OpenAI, MIT licence), ONNX export by Xenova (quantized), run with onnxruntime
on the CPU. CLIP compares a picture with short sentences ("a person wearing a red jacket"); each
attribute group is a choice between a few sentences. The person is cut into regions (head, upper
body, lower body, feet, whole person) so each question is asked about the right part.
"""
import hashlib
import os
from pathlib import Path

import cv2
import numpy as np

COLOURS = ["black", "white", "gray", "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown", "beige"]
COLOUR_WORDS = {"gray": "gray", "beige": "beige or khaki"}

# group id, label, region, options [(value, label, [prompts])], shown only when (group, values)
def _colours(tpl):
    return [(c, c.capitalize(), [tpl.format(c=COLOUR_WORDS.get(c, c))]) for c in COLOURS]


GROUPS = [
    ("gender", "Man / woman", "full", [
        ("man", "Man", ["a photo of a man", "a man walking"]),
        ("woman", "Woman", ["a photo of a woman", "a woman walking"])], None),
    ("hair", "Hair", "head", [
        ("long", "Long hair", ["a person with long hair"]),
        ("short", "Short hair", ["a person with short hair"]),
        ("bald", "Bald", ["a bald person"])], None),
    ("hair_color", "Hair colour", "head", [
        ("black", "Black", ["a person with black hair"]), ("brown", "Brown", ["a person with brown hair"]),
        ("blonde", "Blonde", ["a person with blonde hair"]), ("gray", "Gray / white", ["a person with gray or white hair"]),
        ("red", "Red", ["a person with red hair"])], ("hair", {"long", "short"})),
    ("hat", "Hat", "head", [
        ("yes", "Hat / cap", ["a person wearing a hat", "a person wearing a cap"]),
        ("no", "No hat", ["a person with no hat", "the bare head of a person"])], None),
    ("hat_color", "Hat colour", "head", _colours("a person wearing a {c} hat"), ("hat", {"yes"})),
    ("glasses", "Glasses", "head", [
        ("glasses", "Glasses", ["a person wearing eyeglasses"]),
        ("sunglasses", "Sunglasses", ["a person wearing sunglasses"]),
        ("no", "No glasses", ["a face of a person without glasses"])], None),
    ("top", "Upper clothes", "upper", [
        ("tshirt", "T-shirt", ["a person wearing a t-shirt"]),
        ("shirt", "Shirt", ["a person wearing a button-up shirt"]),
        ("jacket", "Jacket / coat", ["a person wearing a jacket", "a person wearing a coat"]),
        ("sweater", "Sweater / hoodie", ["a person wearing a sweater", "a person wearing a hoodie"]),
        ("dress", "Dress", ["a woman wearing a dress"]),
        ("suit", "Suit", ["a man wearing a suit"]),
        ("vest", "Safety vest", ["a person wearing a high-visibility safety vest"])], None),
    ("top_color", "Upper clothes colour", "upper", _colours("a person wearing a {c} top"), None),
    ("bottom", "Lower clothes", "lower", [
        ("trousers", "Trousers", ["a person wearing trousers"]),
        ("jeans", "Jeans", ["a person wearing jeans"]),
        ("shorts", "Shorts", ["a person wearing shorts"]),
        ("skirt", "Skirt / dress", ["a person wearing a skirt"])], None),
    ("bottom_color", "Lower clothes colour", "lower", _colours("a person wearing {c} trousers"), None),
    ("shoes", "Shoes", "feet", [
        ("sneakers", "Sneakers", ["a person wearing sneakers"]),
        ("shoes", "Shoes", ["a person wearing leather shoes"]),
        ("boots", "Boots", ["a person wearing boots"]),
        ("sandals", "Sandals", ["a person wearing sandals"])], None),
    ("shoes_color", "Shoes colour", "feet", _colours("{c} shoes"), None),
    ("bag", "Bag", "full", [
        ("backpack", "Backpack", ["a person wearing a backpack"]),
        ("handbag", "Handbag / shoulder bag", ["a person carrying a handbag", "a person with a shoulder bag"]),
        ("suitcase", "Suitcase", ["a person pulling a suitcase"]),
        ("no", "No bag", ["a person with nothing in their hands"])], None),
    ("bag_color", "Bag colour", "full", _colours("a person carrying a {c} bag"), ("bag", {"backpack", "handbag", "suitcase"})),
]
# a choice only becomes a tag when CLIP is sure enough; "no" answers are never tags
NOUN = {"hair_color": "hair", "hat_color": "hat", "top_color": "top", "bottom_color": "pants / skirt",
        "shoes_color": "shoes", "bag_color": "bag"}
MIN_P = {"gender": 0.6, "hat": 0.6, "glasses": 0.55, "bag": 0.5}
DEFAULT_MIN_P = 0.3


def taxonomy() -> list[dict]:
    """For the web page: groups and the tags they produce."""
    return [{"id": g, "label": lbl, "options": [{"tag": f"{g}:{v}", "label": l} for v, l, _ in opts if v != "no"],
             "colour": g.endswith("_color"), "noun": NOUN.get(g, "")} for g, lbl, _, opts, _ in GROUPS]


def tag_label(tag: str) -> str:
    g, _, v = tag.partition(":")
    for gid, lbl, _, opts, _ in GROUPS:
        if gid == g:
            for val, l, _ in opts:
                if val == v:
                    if g.endswith("_color"):
                        return f"{l} {NOUN.get(g, g)}"
                    return l
    return tag


MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)


def regions(box, w, h) -> dict:
    """Parts of a standing person (x1, y1, x2, y2), clipped to the picture."""
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    clip = lambda a, b, c, d: (int(max(0, a)), int(max(0, b)), int(min(w, c)), int(min(h, d)))
    return {
        "full": clip(x1 - bw * .08, y1 - bh * .03, x2 + bw * .08, y2 + bh * .02),
        "head": clip(x1 + bw * .1, y1 - bh * .03, x2 - bw * .1, y1 + bh * .24),
        "upper": clip(x1, y1 + bh * .16, x2, y1 + bh * .56),
        "lower": clip(x1, y1 + bh * .48, x2, y1 + bh * .9),
        "feet": clip(x1 - bw * .05, y1 + bh * .83, x2 + bw * .05, y2 + bh * .03),
    }


def to_input(img_bgr) -> np.ndarray:
    """CLIP input: pad to a square (keeps the whole person), 224x224, normalised, CHW."""
    h, w = img_bgr.shape[:2]
    s = max(h, w, 1)
    sq = np.full((s, s, 3), (104, 116, 122), np.uint8)       # CLIP mean colour, BGR
    sq[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = img_bgr
    rgb = cv2.cvtColor(cv2.resize(sq, (224, 224), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
    x = (rgb.astype(np.float32) / 255 - MEAN) / STD
    return x.transpose(2, 0, 1)


def _out(sess, want):
    names = [o.name for o in sess.get_outputs()]
    for n in names:
        if want in n:
            return n
    return names[0]


class Tagger:
    def __init__(self, model_dir: str, cache_dir: str):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = Path(model_dir)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("ANALYTICS_TAG_THREADS", "2"))
        self.vision = ort.InferenceSession(str(d / "clip_vision.onnx"), opts, providers=["CPUExecutionProvider"])
        self.v_in, self.v_out = self.vision.get_inputs()[0].name, _out(self.vision, "image_embeds")
        self.text = self._text_embeddings(d, Path(cache_dir), Tokenizer)

    def _text_embeddings(self, d, cache_dir, Tokenizer) -> dict:
        """Embeddings of every prompt, per group: {group: [(value, unit vector), ...]}. Cached on disk."""
        prompts = [p for _, _, _, opts, _ in GROUPS for _, _, ps in opts for p in ps]
        key = hashlib.sha1(("\n".join(prompts) + (d / "clip_text.onnx").stat().st_mtime.__repr__()).encode()).hexdigest()[:16]
        cache = cache_dir / f"clip_text_{key}.npy"
        if cache.exists():
            emb = np.load(cache)
        else:
            import onnxruntime as ort
            tok = Tokenizer.from_file(str(d / "tokenizer.json"))
            sess = ort.InferenceSession(str(d / "clip_text.onnx"), providers=["CPUExecutionProvider"])
            ins = {i.name for i in sess.get_inputs()}
            out = _out(sess, "text_embeds")
            rows = []
            for p in prompts:              # one at a time: no padding questions
                ids = tok.encode(p).ids
                if not ids or ids[0] != 49406:
                    ids = [49406] + ids
                if ids[-1] != 49407:
                    ids = ids + [49407]
                feed = {"input_ids": np.array([ids[:77]], np.int64)}
                if "attention_mask" in ins:
                    feed["attention_mask"] = np.ones_like(feed["input_ids"])
                rows.append(sess.run([out], feed)[0][0])
            emb = np.array(rows, np.float32)
            cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(cache, emb)
        emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
        res, i = {}, 0
        for g, _, _, opts, _ in GROUPS:
            res[g] = []
            for v, _, ps in opts:
                vec = emb[i:i + len(ps)].mean(0)
                res[g].append((v, vec / np.linalg.norm(vec)))
                i += len(ps)
        return res

    def image_embeddings(self, crops: list[np.ndarray]) -> np.ndarray:
        batch = np.stack([to_input(c) for c in crops]).astype(np.float32)
        e = self.vision.run([self.v_out], {self.v_in: batch})[0]
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def describe(self, frame, box) -> dict:
        """Tags of the person in `box` (x1, y1, x2, y2 in frame pixels)."""
        h, w = frame.shape[:2]
        reg = regions(box, w, h)
        names = [k for k, (a, b, c, d) in reg.items() if c - a >= 8 and d - b >= 8]
        if "full" not in names:
            return {"tags": [], "scores": {}}
        emb = dict(zip(names, self.image_embeddings([frame[b:d, a:c] for a, b, c, d in (reg[n] for n in names)])))
        return decide(self.text, emb)


def decide(text: dict, emb: dict) -> dict:
    """Pick the most likely answer of each group; keep it as a tag when it is sure enough."""
    chosen, scores, tags = {}, {}, []
    for g, _, region, opts, when in GROUPS:
        if region not in emb:
            continue
        if when and chosen.get(when[0]) not in when[1]:
            continue
        vecs = np.stack([v for _, v in text[g]])
        logits = 100.0 * vecs @ emb[region]                   # CLIP's logit scale
        p = np.exp(logits - logits.max())
        p /= p.sum()
        i = int(p.argmax())
        value = text[g][i][0]
        scores[g] = [value, round(float(p[i]), 3)]
        if p[i] >= MIN_P.get(g, DEFAULT_MIN_P):
            chosen[g] = value
            if value != "no":
                tags.append(f"{g}:{value}")
    return {"tags": tags, "scores": scores}
