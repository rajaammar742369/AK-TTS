# VoxForge

Ek self-hosted text-to-speech web app (Flask + `edge-tts`) jis mein voice cloning bhi hai.

## Features
- **English aur Urdu voices** (`ALLOWED_LANGS` se badal sakte hain), language/gender filter, search, favorites, preview
- **Text ki koi limit nahi** - lamba text (ghante bhar ki audio) background job ki tarah chalta hai, progress bar aur cancel ke saath
- Speed, pitch aur volume controls
- `[pause:1]` jaisi pauses (0.5s / 1s / 2s buttons) - ffmpeg chahiye
- Two-speaker dialogue mode (`A:` / `B:` lines)
- **Voice cloning**: apni (ya ijazat wali) awaaz ka 10-60 second ka sample upload ya record karein. Base voice khud chun li jati hai (sample ki gender + text ki zabaan se) aur sample ki pitch se match hoti hai
- **My voices**: cloned awaaz ko naam de kar save karein, baad mein rename / settings badlein / sample replace / delete karein, aur normal TTS ya two-speaker dialogue mein use karein
- `.txt` / `.srt` file open karna, MP3 download, saari clips ka ZIP, dark/light theme

## Run karna

```bash
python -m venv venv
venv\Scripts\activate             # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
python app.py                     # http://localhost:5000
```

ffmpeg (pauses aur cloning ke liye) `pip install -r requirements.txt` ke saath khud aa jata hai.

### Voice cloning on karna (optional)

```bash
pip install -r requirements.txt
pip install -r requirements-cloning.txt
python download_model.py          # ek baar: ~130 MB model download (app khud bhi kar leti hai)
python app.py
```

ffmpeg alag se install karne ki zaroorat nahi: `imageio-ffmpeg` (requirements.txt mein) khud la deta hai.

Model pehle Hugging Face (official OpenVoice V2) se, phir MyShell ke S3 se download hota hai.
Agar dono fail hon (internet/firewall), to ye 2 files browser se download karke `~/.voxforge/models/openvoice/converter/` folder mein (Windows: `C:\Users\<naam>\.voxforge\models\openvoice\converter\`) rakh dein:

- https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/main/converter/config.json
- https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/main/converter/checkpoint.pth

Apna mirror use karna ho: `OPENVOICE_CONVERTER_BASE=https://.../folder` (jis mein dono files hon).

**Cloning kaise kaam karti hai:** pehle text ko ek "base voice" (Edge neural voice) bolti hai, phir OpenVoice ka tone-color converter
us awaaz ka *timbre* aapke sample jaisa kar deta hai.

- Base voice **automatic** chuni jati hai: sample ki gender (pitch se pehchan) aur text ki zabaan (Urdu ya English). Chahein to
  "Pick the base voice automatically" band kar ke khud chun sakte hain.
- **Match pitch**: base voice ki pitch ko sample ki pitch ke qareeb kiya jata hai, warna output base voice jaisa lagta hai.
- **Similarity strength**: 0.3-0.8 aazmayein. Zyada = sample se zyada milti-julti, kam = saaf awaaz.
- **Limit (zaroori):** converter sirf awaaz ka tone badalta hai. **Lehja (accent), talaffuz aur andaz base voice se aate hain**, sample se nahi.
  Isliye 100% copy nahi hoti. Accent/andaz bhi copy karne ke liye alag (bhari, GPU wali) cloning engine chahiye.
- Sample saaf, ek hi insaan ka, bina music/shor ke, 20-60 second ka ho.

### My voices (saved voices)
"Voice cloning" tab mein sample dein, permission tick karein, naam likh kar **Save voice** dabayein. Phir wo "My voices" list mein aati hai
(har mode mein nazar aati hai):
- click = us awaaz se TTS; ▶ = asli sample sunein (sunte waqt ■ ban jata hai, dobara dabane se ruk jata hai); ✎ = editor
- Editor mein: naam badalna (rename), base voice, strength, speed, pitch, match pitch, **Replace sample**, **Delete**
- Dialogue mode mein speaker A ya B ko saved voice de sakte hain
- Files `~/.voxforge/saved_voices/` mein hoti hain (har voice ka apna folder: `sample.wav` + `meta.json`). Isay backup/copy kar sakte hain.

**Speed:** cloning CPU par asli waqt se dheemi ho sakti hai (GPU ho to `torch` ka CUDA build install karein, khud use hoga).
Ghante bhar ki cloned audio CPU par kaafi der le sakti hai; chhote text se pehle test karein.

### Production

Jobs memory mein rehti hain, isliye **ek hi worker process** chalayein (threads zyada rakh sakte hain):

```bash
gunicorn -w 1 --threads 8 --timeout 300 -b 0.0.0.0:8000 app:app
```

## Settings (environment variables)
| Variable | Default | Matlab |
|---|---|---|
| `MAX_CHARS` | 0 | 0 = koi limit nahi; koi number likhein to utni limit lag jati hai |
| `ALLOWED_LANGS` | en,ur | Sirf in languages ki voices dikhein (comma se alag) |
| `CHUNK_CHARS` | 1500 | Ek edge-tts request mein kitna text |
| `CONCURRENCY` | 3 | Ek saath kitni requests (zyada rakhne par Microsoft rate-limit kar sakta hai) |
| `MAX_RUNNING_JOBS` | 2 | Ek waqt mein kitni jobs chalen |
| `JOB_TTL_HOURS` | 3 | Tayyar audio server par kitni der rakhi jaye |
| `RATE_LIMIT_PER_MIN` | 30 | Per IP naye jobs per minute |
| `PORT` | 5000 | Server port |
| `DEBUG` | 0 | `1` = debug mode |

Windows par env variable aise set karein: `set MAX_CHARS=0` phir `python app.py`.

## Project structure
```
voxforge/
├── app.py                    # Flask backend, chunking, background jobs
├── cloning.py                # voice cloning pipeline, pitch estimate, model download
├── download_model.py         # model ek baar download karne ki script
├── ffmpeg_util.py            # ffmpeg khud dhoondta hai
├── openvoice_lite/           # OpenVoice tone-color converter (MIT), trimmed copy
├── requirements.txt
├── requirements-cloning.txt  # optional (cloning)
├── templates/index.html
└── static/ (style.css, app.js)
```

## Zaroori notes
- TTS Microsoft Edge ki online voice service (unofficial `edge-tts` library) se hoti hai: internet chahiye, aur Microsoft ki terms/availability
  par depend karti hai. Public ya commercial service ke liye official **Azure Speech** (apni API key ke saath) behtar hai - `_synth()` badalna kaafi hai.
  Bahut lambay text par Microsoft rate-limit kar sakta hai; `CONCURRENCY` kam kar ke dekhein.
- Voice cloning sirf **apni ya jis ki ijazat ho** us ki awaaz par karein. Kisi aur ki nakal, dhoka ya misleading content ke liye istemal na karein.
- `openvoice_lite/` MyShell.ai ke OpenVoice (MIT licence) ka chhota hissa hai, licence `LICENSE-OpenVoice` mein hai.

## Data kahan save hota hai (naya folder banane par bhi safe)
Cloning model aur **My voices** project folder ke bahar, yahan rakhe jate hain:

- Windows: `C:\Users\<aap ka naam>\.voxforge\`  (is mein `models\` aur `saved_voices\`)
- Linux/Mac: `~/.voxforge/`

Is liye nayi zip kisi bhi folder mein extract kar ke chalayein, model dobara download nahi hoga aur saved voices wahin milengi.
Agar purane project folder mein `models\` ya `saved_voices\` hon to pehli baar chalane par khud yahan import ho jate hain.
Jagah badalni ho to `VOXFORGE_DATA` environment variable set karein.

## Fresh install
Zip extract karein, us folder mein terminal kholein aur `pip install -r requirements.txt`, `pip install -r requirements-cloning.txt`, phir `python app.py` chalayein.
Model download na ho (internet/firewall) to error mein diye gaye 2 links browser se download kar ke
`C:\Users\<aap ka naam>\.voxforge\models\openvoice\converter\` mein rakh dein (ya purane folder ka `models\openvoice\converter` copy kar dein).
