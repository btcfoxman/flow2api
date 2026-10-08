# Flow image upscale and Veo 3.1 RPC evidence (2026-10-09)

Captured from a manually operated Flow project through passive CDP listening.
Only RPC IDs, model enum values, structure, and HTTP status were retained;
prompts, media IDs, cookies, tokens, and image bodies were discarded.

| Operation | RPC | Captured upstream result | Relevant request fields |
| --- | --- | --- | --- |
| Base image | `ogiZ0b` | HTTP 200 | `GEM_PIX_2` |
| Image upscale to 2K | `SPrCad` | HTTP 200 | `[sourceMediaId, 1, projectContext]` |
| Veo 3.1 Fast text, portrait | `YhhmEf` | HTTP 200 | `veo_3_1_t2v_fast_portrait`, aspect `1` |
| Veo 3.1 Fast text, landscape | `YhhmEf` | HTTP 200 | `veo_3_1_t2v_fast`, aspect `2` |
| Veo 3.1 Fast single first frame, portrait | `eb1hJf` | HTTP 200 | `veo_3_1_i2v_s_fast_portrait`, aspect `1`, start frame and crop |
| Veo 3.1 Fast first and last frames, landscape | `nprQif` | HTTP 200 | `veo_3_1_i2v_s_fast_fl`, aspect `2`, both frames and crops |

The `SPrCad` response has a media row followed by base64 JPEG data. Its
media ID may differ from the source media ID, so the response is bound by
project ID and checked for valid JPEG bytes. The 4K enum value `2` is also
documented by the independent FlowKit protocol implementation; no 4K page
submission was made in this capture.

The native Flow path must fail an upscale request if the requested resolution
cannot be delivered. Returning the original 1K image as a successful 2K or
4K result would mislead the caller.
