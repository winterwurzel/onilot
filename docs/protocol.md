# Tolino reader protocol (firmware 5.x, Tolino mode)

What the reader app expects from its backend, as needed to implement an interoperable
server. Verified against a Tolino Vision Color on firmware **5.18.264769**; other 5.x
versions are likely similar but untested.

All paths below are relative to the REST base, i.e. the URL `tools/patch_libnickel.py`
writes into the reader (`ONILOT_PUBLIC_URL`). JSON everywhere unless noted.

## Request headers

| Header | When |
|---|---|
| `Authorization: Bearer <access_token>` | all authenticated REST calls |
| `t_auth_token: <access_token>` | some older-style calls (device registration, user id) |
| `Hardware-Id`, `Reseller-Id`, `client_type` (e.g. `TOLINO_VISION_7`), `client_version` (e.g. `5.18.264769`) | most calls |

Book and cover downloads carry **no** token. Use capability URLs (onilot signs them).

## Setup and login

| Step | Request | Response |
|---|---|---|
| connectivity | `GET /online-check?responseStatus=204` | `204` |
| countries | `GET /v1/reseller-group/devices/{hw}/countries?languageCode=de` | `{"resellerCountryResponse": {"resellerCountryGroups": [{"languageCode": "de", "resellerCountries": [{"country": "Deutschland", "countryCode": "DE"}]}]}}` |
| resellers | `GET /v1/reseller-group/devices/{hw}/resellers?client_type=…&countryCode=…&languageCode=…` | `{"resellerSelectionResponse": {"resellerSelection": [{"resellerId": 1, …}]}}`. With one entry the selection screen is skipped |
| reseller config | `GET /v1/resellers/{id}/client-types/{type}/config` | `{"reseller_id": 1, "config": {…}}`, see below |
| login page | webview `GET {URL_OAUTH_AUTHORIZATION}&client_id={OAUTH_CLIENT_ID}` | HTML; on success redirect to `epublishing://login?code=<code>` |
| token | `POST {URL_OAUTH_ACCESSTOKEN}` form `code`, `grant_type=authorization_code`, `redirect_uri=epublishing://login`, `client_id` | `{"access_token", "refresh_token", "expires_in"}` |
| refresh | same URL, `grant_type=refresh_token`, `refresh_token` | same; the reader refreshes before expiry |
| user id | `GET /v1/devices/{hw}/userid` | `{"userId": …}` (format not confirmed; accepted) |
| device registration | `POST /v1/devices` `{"initAppRequest": {…}}` | `{"initAppResponse": {"userId": …, "deviceKey": …}}` |
| logout | `POST {URL_OAUTH_REVOKETOKEN}` | `200` |

**Access token:** the reader splits it on `.` and base64-decodes the middle part, reading
`sub` as the user/bookshelf id. It never checks a signature, but it must be JWT-shaped.

**Reseller config** (`config` object): the reader stores these and uses them for login and
the shop tab. Relevant keys:

| Key | Use |
|---|---|
| `URL_OAUTH_AUTHORIZATION` | login page; the reader appends `&client_id=…`, so it must already contain a `?` |
| `URL_OAUTH_ACCESSTOKEN`, `URL_OAUTH_REVOKETOKEN`, `OAUTH_CLIENT_ID` | OAuth |
| `STRING_BRAND_NAME`, `URL_RESELLER_LOGO` | branding |
| `SHOP_BASE`, `URL_SHOP_EBOOK_START_PAGE`, `URL_SHOP_EBOOK_SEARCH` (`%s` = query) | shop webview. Some entry points request `SHOP_BASE` and the start page concatenated |
| `FAMILY_SHARING_ACTIVE`, `KDRM_ACTIVATED`, `LCP_ACTIVATED`, `METRICS_ACTIVATED`, `IS_ONLEIHE_LINK_ENABLED`, … | feature flags; `false` |

## Library

`GET /v8/inventory?fullResponse=true&size=100&page=0&contentTypeFilter=EBOOK,AUDIOBOOK&…`

```json
{
  "page": {"content": [<content item>], "numberOfElements": 1},
  "deletedContent": [{"uuid": "..."}],
  "lastModifiedDate": 1791728118318
}
```

- The reader sends `lastModifiedDate` back as `If-Modified-Since`. **Answer `304` when
  nothing changed.** A `200` makes it re-import every item, which resets read status and
  reading position.
- More pages are requested while elements received equals the page size.

**Content item** (minimum that works):

```json
{
  "uuid": "…", "publicationId": "…", "transactionId": "…",
  "title": "…", "language": "de", "publisher": "…",
  "contentFormat": "application/epub+zip",
  "contentType": "EBOOK",
  "contentSources": ["PURCHASED"],
  "contentIsPreview": false,
  "purchasedDate": 1791728118318,
  "resellerId": 1, "resellerName": "…",
  "authors": [{"name": "…", "type": "AUTHOR"}],
  "fileResources": [{"type": "COVER_IMAGE", "resource": "<cover url>"}],
  "readableContentInfo": {"renderingEngine": "READIUM_1"}
}
```

- **`contentFormat` must be a MIME type** (`application/epub+zip`). The reader uses it as
  the book's file type; with `EPUB` it never recognizes the book as EPUB and never stores
  reading positions.
- Enum values: `contentType` `EBOOK`/`AUDIOBOOK`; `contentSources` `PURCHASED`,
  `USER_UPLOAD`, `FAMILY_SHARED`, `HANDSHAKE`, `LOANED`, `SKOOBE_BORROWED`, `PREVIEW`;
  `renderingEngine` `FGRE`/`READIUM_1`; author `type` `AUTHOR`/`READER`/`COMPOSER`.
- The cover `resource` URL gets `?size=WS-B04` appended by the reader and is stored
  permanently. Keep signatures in the path and don't let cover URLs expire.

## Download

`GET /v1/inventory/{uuid}/downloadinfo` returns
`{"contentUrl": "<url>", "format": "application/epub+zip", "fileSize": 192387}`.
The reader then downloads `contentUrl` without a token. Vendor DRM uses additional
`encryptionInfo`/`drmKeys` objects, which onilot does not implement.

## Reading progress

`PATCH /v4/reading-metadata?paths=publications,audiobooks`

Request: `{"revision": "<last revision the reader saw>", "patches": [...]}`, JSON-patch style.

A new local bookmark:

```json
{"op": "add", "path": "/publications/<uuid>/bookmark",
 "value": {"currentPosition": "6", "lastPosition": "122", "modified": 1791730218405,
           "position": "OEBPS/chapter.xhtml#point(/1/3/1/2/2:0)", "progress": 0.049,
           "transientId": 2}}
```

`currentPosition`/`lastPosition` are page numbers **as strings**; `position` is an
Adobe-style location; `modified` is epoch milliseconds.

Response: `{"revision": "<new>", "patches": [...], "conflicts": []}`. Rules found on the device:

1. **Confirm each `add`** with `{"op": "add", "path": "/publications/<uuid>/bookmark/<id>",
   "value": {…, "transientId": "2", "revision": "<new>"}}`. The id goes at the end of the
   path, and **`transientId` must be a string**: a number is read as 0 and the echo is
   stored as an extra bookmark. Without a confirmation the reader re-sends the `add` on
   every sync.
2. Afterwards the reader sends changes as `replace` on `…/bookmark/<id>`. **Echo the
   accepted state** (`replace`, same path) or it re-sends the change on every sync.
3. **Any response to incoming patches needs a new revision.** If the revision equals the
   reader's own, it logs "already up-to-date" and ignores the whole response, confirmations
   included.
4. **Server to reader:** include `replace`/`remove` patches for documents changed after the
   reader's revision. The reader applies them and opens the book at the new position.

Other document types under the same scheme: `/dogears`, `/comments`, `/tags`.

## Uploads of sideloaded books

The reader pushes books copied over USB to the cloud automatically:
`POST /v1/inventory/user-uploads`, multipart field `file` (the filename gets a second
`.epub`). Response:

```json
{"metadata": {"inventoryUuid": "…", "title": "…", "subtitle": "", "language": "de",
              "isbn": "", "publisher": "…", "issued": 0,
              "format": "application/epub+zip", "authors": "Name, Name"}}
```

`authors` is a plain string. The reader then posts the cover to
`/v1/inventory/user-uploads/{uuid}/cover`, re-fetches the library, and moves the local
reading position to the cloud copy as a bookmark `add`.

## Account pages (Settings > account)

"Link Library" opens `URL_HANDSHAKE` and "Device Management" opens `URL_DEVICE_MANAGEMENT`
from the reseller config in a webview (the latter with `?action=logindevicelimit&lang=…#devices`
appended). Without these keys the reader falls back to the vendor's management site. The
reader injects into the page:

```js
window.handshake_config = { token: "<access token>", wdycf: "handshakeCancel",
                            libjump: "handshakeFinish", deviceid: "<hardware id>" };
```

Navigating to any URL containing `handshakeFinish` closes the dialog and starts a sync;
`handshakeCancel` only closes it (the request still reaches the server). onilot uses this
for a library page with a "Sync now" button and a device list with "sign out all readers".

## Home screen recommendations

`GET /v2/recommendations?count=12&context=homescreen&type=ebook` returns a top-level array
of `{"publicationId", "title", "author", "cover_url", "description", "publicationType",
"shop_url"}`. Tapping an entry opens `shop_url` in the shop webview.
