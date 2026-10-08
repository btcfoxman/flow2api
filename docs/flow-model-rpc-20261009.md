# Flow image upscale and Veo 3.1 RPC evidence (2026-10-09)

Captured from a manually operated Flow project through passive CDP listening.
Only RPC IDs, model enum values, structure, and HTTP status were retained;
prompts, media IDs, cookies, tokens, and image bodies were discarded.

| Operation | RPC | Captured upstream result | Relevant request fields |
| --- | --- | --- | --- |
| Base image | `ogiZ0b` | HTTP 200 | `GEM_PIX_2` |
| Gemini 3.1 Flash base image | `ogiZ0b` | HTTP 200 | `BELUGA` |
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

The current Gemini 3.1 Flash image key is `BELUGA` (manually captured on
2026-10-09). The service still used the older `NARWHAL` key, which received
gRPC 5 / HTTP 404 in a live 3.5 pre request. Older September captures of
`NARWHAL` remain historical evidence; the public Flash aliases now select
`BELUGA`.

The 3.5 pre account #20 page currently labels this selection "Nano Banana
2.1". Three text-only `BELUGA` submissions from that browser returned an
HTTP 200 batchexecute envelope containing gRPC status 7 rather than image
media. A visible task card alone is therefore not proof of a successful
generation. The same account did successfully return media for `GEM_PIX_2`
with one reference image. The page displayed "unusual activity" for the
`BELUGA` attempts. The precise wire-level public reason was not retained by
the sanitized listener, so this does not establish a general model outage.

For text-only `ogiZ0b`, the current Flow page sends `null` in the reference
image slot (entry field 3). The service had sent an empty array. The encoder
now matches the captured page request; the field remains an array when there
are actual reference images. A live service request using the old empty-array
shape received `PUBLIC_ERROR_UNSAFE_GENERATION`; the upstream response alone
does not prove this field caused that verdict.

Account #50 provided a same-account and same-project comparison on 3.5 pre.
The Flow webpage returned image media for a text-only `BELUGA` request, while
the deployed service's direct `ogiZ0b` request received gRPC 3 /
`PUBLIC_ERROR_UNSAFE_GENERATION` with an innocuous prompt. A direct RPC made
from that account's own Chrome project page also received gRPC 3. The current
page's successful request did not include the historical `x-browser-validation`
header, so adding that header is not supported by this comparison. The webpage
used a 32-bit-range seed and uppercase UUIDs for request IDs; the service
encoder now matches these observed fields. Their individual effect on the
upstream verdict is not established. Subsequent repeated webpage submissions
also began receiving gRPC 3, so further paid canaries should wait for the
account to cool down.

After aligning the ID and seed fields, a 3.5 pre service request for a 1K
text-only `BELUGA` image completed on account #6. The next 2K request was
assigned to account #52 on a different proxy exit and was rejected before
upscaling with gRPC 7 / `PUBLIC_ERROR_UNUSUAL_ACTIVITY` (HTTP 429). This does
not establish an upscale protocol failure. The native browser recorded the
proxy risk, but image account selection had only applied that cooldown to
video requests. Image routing now filters cooling exits, and restart recovery
also replays image request results when rebuilding proxy risk history.

The native Flow path must fail an upscale request if the requested resolution
cannot be delivered. Returning the original 1K image as a successful 2K or
4K result would mislead the caller.
