# Source audit — 2026-10-19

Read-only live audit: 191 complete reads, 5 partial reads, 10 disabled sources; 206 configured sources total.

Netflix: 395 unique IDs matched its official Eightfold count of 395. Pinterest: 154 unique IDs matched Greenhouse meta.total of 154. Pagination and source validation were exercised without sending notifications. Counts are a point-in-time snapshot, not a guarantee of all openings across the web.

Complete means the configured endpoint returned without detected omissions. This does not independently verify that a query/facet includes every employer role. Disabled endpoints were inventoried, not fetched. LinkedIn page limits and malformed Workday records remain explicit gaps.

Follow-up checks: repeated unpaced Netflix reads hit HTTP 429. With one-second
page pacing, the final check recovered all **395 postings in 52.8 seconds with
no warnings**. Pinterest remained at **154 complete postings**. Salesforce
still rate-limited a follow-up read at 100 postings with 0.5-second pacing;
its configuration now uses one-second pacing, but complete recovery is not
claimed. Provider Retry-After is preserved across partial-page failures.

Offline migration of a copy of production v4 state preserved **380,067 job IDs**
and all deduplication keys through a v5 import/export round trip. No live
notification sends were used for validation.

| Source | Adapter | Status | Jobs | Notes |
|---|---|---|---:|---|
| 1password | ashby | complete | 76 |  |
| abridge | ashby | complete | 44 |  |
| adobe | workday | complete | 306 |  |
| adobe_university | workday | complete | 21 |  |
| affirm | greenhouse | complete | 215 |  |
| airbnb | greenhouse | complete | 177 |  |
| airtable | greenhouse | complete | 4 |  |
| akuna | greenhouse | complete | 40 |  |
| alloy | greenhouse | complete | 25 |  |
| amazon | amazon_jobs | complete | 24 |  |
| amazon_newgrad | amazon_jobs | complete | 1735 |  |
| amplitude | ashby | complete | 40 |  |
| anduril | greenhouse | complete | 2531 |  |
| anthropic | greenhouse | complete | 667 |  |
| aqr | greenhouse | complete | 56 |  |
| asana | greenhouse | complete | 109 |  |
| assemblyai | greenhouse | complete | 15 |  |
| attio | ashby | complete | 35 |  |
| aurora | ashby | complete | 101 |  |
| autodesk | workday | complete | 310 |  |
| baseten | ashby | complete | 104 |  |
| betterment | greenhouse | complete | 25 |  |
| block | greenhouse | complete | 218 |  |
| bmo | workday | complete | 968 |  |
| braintrust | ashby | complete | 28 |  |
| brex | greenhouse | complete | 261 |  |
| browserbase | ashby | complete | 9 |  |
| calendly | greenhouse | complete | 10 |  |
| canada_intern26 | github_md | complete | 263 |  |
| capital_one | workday | complete | 1967 |  |
| capital_one_early | workday | complete | 7 |  |
| cartesia | ashby | complete | 26 |  |
| character_ai | ashby | complete | 13 |  |
| checkr | greenhouse | complete | 42 |  |
| chime | greenhouse | complete | 71 |  |
| cibc | workday | complete | 525 |  |
| citadel | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| citadel_sec | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| clickhouse | ashby | complete | 198 |  |
| cloudflare | greenhouse | complete | 436 |  |
| cockroach_labs | greenhouse | complete | 17 |  |
| cohere | ashby | complete | 115 |  |
| coinbase | greenhouse | complete | 227 |  |
| comcast | workday | complete | 656 |  |
| commure | ashby | complete | 81 |  |
| crusoe | ashby | complete | 349 |  |
| cursor | ashby | complete | 134 |  |
| cvrve_newgrad | github_listings | complete | 631 |  |
| databricks | greenhouse | complete | 901 |  |
| datadog | greenhouse | complete | 429 |  |
| decagon | ashby | complete | 135 |  |
| deepmind | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| derec4_newgrad27 | github_md | complete | 2405 |  |
| descript | greenhouse | complete | 10 |  |
| discord | greenhouse | complete | 47 |  |
| disney | workday | complete | 410 |  |
| disney_early | workday | complete | 29 |  |
| doordash | greenhouse | complete | 454 |  |
| dropbox | greenhouse | complete | 18 |  |
| drw | greenhouse | complete | 165 |  |
| duolingo | greenhouse | complete | 63 |  |
| ebay | workday | complete | 269 |  |
| elastic | greenhouse | complete | 377 |  |
| elevenlabs | ashby | complete | 154 |  |
| faire | greenhouse | complete | 81 |  |
| fastly | greenhouse | complete | 50 |  |
| figma | greenhouse | complete | 140 |  |
| fireblocks | greenhouse | complete | 26 |  |
| five_rings | greenhouse | complete | 17 |  |
| fivetran | greenhouse | complete | 177 |  |
| flexport | greenhouse | complete | 209 |  |
| flow_traders | greenhouse | complete | 45 |  |
| gemini | greenhouse | complete | 31 |  |
| geneva_trading | greenhouse | complete | 14 |  |
| gitlab | greenhouse | complete | 228 |  |
| glean | greenhouse | complete | 134 |  |
| glossier | greenhouse | complete | 21 |  |
| gopuff | lever | complete | 808 |  |
| gts | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| gusto | greenhouse | complete | 101 |  |
| harvey | ashby | complete | 341 |  |
| heygen | greenhouse | complete | 22 |  |
| hightouch | hightouch | complete | 83 |  |
| hinge_health | ashby | complete | 75 |  |
| hp | workday | complete | 841 |  |
| hrt | greenhouse | complete | 86 |  |
| imbue | greenhouse | complete | 2 |  |
| imc | greenhouse | complete | 171 |  |
| instacart | greenhouse | complete | 122 |  |
| intel | workday | complete | 726 |  |
| intel_early | workday | complete | 94 |  |
| jane_street | janestreet | complete | 221 |  |
| jump_trading | greenhouse | complete | 101 |  |
| lambda_labs | ashby | complete | 94 |  |
| launchdarkly | greenhouse | complete | 54 |  |
| linear | ashby | complete | 28 |  |
| linkedin_canada | linkedin | partial | 196 | LinkedIn query 'software engineer intern' reached its page limit; LinkedIn query 'software engineer new grad' reached its page limit |
| linkedin_quant | linkedin | partial | 262 | LinkedIn query 'quantitative trading intern' reached its page limit; LinkedIn query 'quantitative research intern' reached its page limit; LinkedIn query 'quantitative trader 2027' reached its page limit |
| linkedin_us | linkedin | partial | 398 | LinkedIn query 'software engineer intern' reached its page limit; LinkedIn query 'software engineer new grad' reached its page limit; LinkedIn query 'software developer intern' reached its page limit; LinkedIn query '2027 software engineer' reached its page limit; LinkedIn query 'machine learning intern' reached its page limit |
| lithic | greenhouse | complete | 11 |  |
| lyft | greenhouse | complete | 183 |  |
| marqeta | marqeta | complete | 45 |  |
| mastercard | workday | partial | 1117 | posting without a stable ID at offset 720 |
| mastercard_early | workday | complete | 17 |  |
| mercor | ashby | complete | 110 |  |
| mercury | greenhouse | complete | 77 |  |
| microsoft | microsoft | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| midjourney | ashby | complete | 17 |  |
| mixpanel | greenhouse | complete | 47 |  |
| modal | ashby | complete | 43 |  |
| mongodb | greenhouse | complete | 396 |  |
| nasdaq | workday | complete | 168 |  |
| netflix | eightfold | complete | 395 |  |
| neuralink | greenhouse | complete | 79 |  |
| notion | ashby | complete | 133 |  |
| nuro | greenhouse | complete | 107 |  |
| nvidia | workday | complete | 243 |  |
| nwfintech_quant27 | github_md | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| offseason_fall26 | github_md | complete | 606 |  |
| old_mission | greenhouse | complete | 0 |  |
| openai | ashby | complete | 806 |  |
| openevidence | ashby | complete | 10 |  |
| optiver_us | greenhouse | complete | 167 |  |
| oscar_health | greenhouse | complete | 249 |  |
| palantir | lever | complete | 306 |  |
| paypal | workday | complete | 340 |  |
| pdt_partners | greenhouse | complete | 11 |  |
| peloton | greenhouse | complete | 54 |  |
| perplexity | ashby | complete | 134 |  |
| pinterest | greenhouse | complete | 154 |  |
| plaid | ashby | complete | 132 |  |
| planetscale | greenhouse | complete | 11 |  |
| point72 | greenhouse | complete | 215 |  |
| poolside | ashby | complete | 2 |  |
| postman | workday | complete | 25 |  |
| qube_rt | greenhouse | complete | 201 |  |
| ramp | ashby | complete | 167 |  |
| rbc | phenom | complete | 140 |  |
| rbc_early | phenom | complete | 64 |  |
| reddit | greenhouse | complete | 152 |  |
| retool | ashby | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| rippling | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| robinhood | greenhouse | complete | 151 |  |
| roblox | greenhouse | complete | 262 |  |
| runpod | ashby | complete | 28 |  |
| runway | ashby | complete | 3 |  |
| salesforce | workday | partial | 60 | page at offset 60: HTTPError: 429 Client Error: Too Many Requests for url: https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs |
| salesforce_early | workday | complete | 24 |  |
| samsara | greenhouse | complete | 266 |  |
| scale_ai | greenhouse | complete | 181 |  |
| schonfeld | greenhouse | complete | 70 |  |
| seatgeek | greenhouse | complete | 37 |  |
| servicenow | smartrecruiters | complete | 693 |  |
| shield_ai | lever | complete | 643 |  |
| sierra | ashby | complete | 187 |  |
| sig | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| simplify_intern26 | github_listings | complete | 4798 |  |
| simplify_intern27 | github_listings | complete | 4798 |  |
| simplify_newgrad | github_listings | complete | 2984 |  |
| smartsheet | greenhouse | complete | 92 |  |
| sndsh_intern27 | github_md | complete | 135 |  |
| sofi | greenhouse | complete | 50 |  |
| spacex | greenhouse | complete | 2768 |  |
| speedy_ai | github_md | complete | 770 |  |
| speedy_swe | github_md | complete | 876 |  |
| spotify | lever | complete | 82 |  |
| squarepoint | greenhouse | complete | 91 |  |
| squarespace | greenhouse | complete | 26 |  |
| starburst | greenhouse | complete | 31 |  |
| stripe | greenhouse | complete | 727 |  |
| suno | ashby | complete | 68 |  |
| supabase | ashby | complete | 53 |  |
| sweetgreen | greenhouse | complete | 66 |  |
| target | workday | complete | 2000 |  |
| td | workday | complete | 1643 |  |
| together_ai | greenhouse | complete | 78 |  |
| tower_research | greenhouse | complete | 97 |  |
| twilio | greenhouse | complete | 134 |  |
| two_sigma | greenhouse | disabled | — | No direct monitoring; discovery coverage not guaranteed. |
| typeface | greenhouse | complete | 19 |  |
| upstart | greenhouse | complete | 91 |  |
| vansh_intern26 | github_listings | complete | 531 |  |
| vansh_intern27 | github_listings | complete | 531 |  |
| vansh_newgrad27 | github_listings | complete | 631 |  |
| vanta | ashby | complete | 85 |  |
| vatic_labs | greenhouse | complete | 7 |  |
| vercel | greenhouse | complete | 93 |  |
| verkada | greenhouse | complete | 317 |  |
| virtu | greenhouse | complete | 49 |  |
| voleon | ashby | complete | 58 |  |
| waabi | lever | complete | 94 |  |
| warp | ashby | complete | 21 |  |
| waymo | greenhouse | complete | 366 |  |
| wealthsimple | ashby | complete | 44 |  |
| webflow | ashby | complete | 17 |  |
| workday_hq | workday | complete | 387 |  |
| worldquant | greenhouse | complete | 95 |  |
| writer | ashby | complete | 47 |  |
| xai | greenhouse | complete | 307 |  |
| zapply_ca_intern | github_md | complete | 146 |  |
| zapply_ca_jobs | github_md | complete | 486 |  |
| zapply_intern27 | github_md | complete | 600 |  |
| zapply_newgrad27 | github_md | complete | 588 |  |
| zapply_swe_newgrad | github_md | complete | 466 |  |
| zoom | workday | complete | 81 |  |
| zoox | lever | complete | 238 |  |
