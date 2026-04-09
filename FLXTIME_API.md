# FlxTime API Integration Guide

## Overview

This document describes the dedicated API endpoint provided for FlxTime to check miner existence in the FryNetworks hardware database.

## Miner Key Format

Miner keys in the FryNetworks system follow a specific format that identifies the miner type and provides a unique identifier.

### Format Structure

```
[PREFIX]-[UNIQUE_IDENTIFIER]
```

**Miner Type Prefixes:**
- `BM-` - Bandwidth Miner
- `IDM-` - Indoor Decibel Miner
- `ODM-` - Outdoor Decibel Miner
- `ISM-` - Indoor Satellite Miner
- `OSM-` - Outdoor Satellite Miner
- `RDN-` - Reward Decentralization Node
- `SDN-` - Storage Decentralization Node
- `SVN-` - Storage Validator Node
- `AEM-` - AI Edge Miner
- `IRM-` - Indoor Radiation Miner

**Unique Identifier:**
- 32-character alphanumeric string (uppercase)
- Contains letters (A-Z) and numbers (0-9)
- Example: `A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6`

### Complete Examples

```
ISM-A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6
BM-9Z8Y7X6W5V4U3T2S1R0Q9P8O7N6M5L4
AEM-F1E2D3C4B5A6978654321098765432AB
RDN-8C7B6A9D8E7F6G5H4I3J2K1L0M9N8O7
```

### What We Expect

When using the API endpoint, provide the **complete miner key** including:
1. The appropriate miner type prefix (BM-, ISM-, AEM-, RDN-, etc.)
2. The hyphen separator (-)  
3. The full 32-character unique identifier

**Important:** The miner key is case-sensitive and must be provided exactly as registered in the FryNetworks system.

## Authentication

Your API requests must include a Bearer token in the Authorization header:

```http
Authorization: Bearer YOUR_FLXTIME_TOKEN
```

**Important:** Keep your bearer token secure. Do not commit it to version control or expose it in client-side code.

## Base URL

**Production:** `https://hardwareapi.frynetworks.com`

## Endpoint

### Check Miner Existence

Verify whether a miner key exists in the credentials database.

**Endpoint:** `GET /credentials/{miner_key}/exists`

**Rate Limit:** 100 requests per minute per IP address

**Parameters:**
- `miner_key` (path parameter, required) - The full miner key to check

**Response:**
```json
{
  "exists": true
}
```
or
```json
{
  "exists": false
}
```

**Status Codes:**
- `200 OK` - Request successful, check the `exists` field in response
- `401 Unauthorized` - Missing or invalid bearer token
- `429 Too Many Requests` - Rate limit exceeded (see Rate Limiting section)
- `500 Internal Server Error` - Server configuration error

## Usage Examples

### cURL

```bash
curl -X GET "https://hardwareapi.frynetworks.com/credentials/ISM-A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6/exists" \
  -H "Authorization: Bearer YOUR_FLXTIME_TOKEN"
```

## Error Handling

### Authentication Errors

**Missing Token:**
```json
{
  "detail": "Missing authentication token"
}
```
Status: `401 Unauthorized`

**Invalid Token:**
```json
{
  "detail": "Invalid authentication token"
}
```
Status: `401 Unauthorized`

### Rate Limiting

**Rate Limit Exceeded:**
```json
{
  "error": "Rate limit exceeded: 100 per 1 minute"
}
```
Status: `429 Too Many Requests`

**Rate Limit Headers:**
Each response includes headers showing your current rate limit status:
- `X-RateLimit-Limit` - Maximum requests allowed in the time window
- `X-RateLimit-Remaining` - Requests remaining in current window
- `X-RateLimit-Reset` - Unix timestamp when the rate limit resets
- `Retry-After` - Seconds to wait before retrying (included in 429 responses)

**Example Response Headers:**
```http
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1729612345
```

## Rate Limiting Details

The FlxTime API endpoint is rate limited to **100 requests per minute per IP address**.

### How It Works

- Rate limits are applied per source IP address
- The time window is a rolling 1-minute period
- If you exceed the limit, you'll receive a `429 Too Many Requests` response
- Wait for the time specified in the `Retry-After` header before making new requests

### Strategies to Stay Within Limits

1. **Monitor Headers:** Check `X-RateLimit-Remaining` to track your usage
2. **Implement Backoff:** If you receive a 429, wait before retrying
3. **Batch Requests:** If checking multiple miners, space out requests over time
4. **Cache Results:** Cache existence checks to reduce API calls
5. **Request Limit Increase:** Contact FryNetworks if you need higher limits

### Best Practices

1. **Retry Logic:** Implement exponential backoff for transient errors (5xx status codes)
2. **Rate Limit Handling:** Always check for 429 status and respect the `Retry-After` header
3. **Monitor Headers:** Track `X-RateLimit-Remaining` to avoid hitting limits
4. **Timeout:** Set reasonable HTTP timeouts (recommended: 10-30 seconds)
5. **Rate Limiting:** Stay within 100 requests/minute or contact FryNetworks for higher limits
6. **Logging:** Log failed requests with timestamps for debugging
7. **Token Security:** 
   - Store tokens in environment variables or secure vaults
   - Never log or display tokens in plaintext
   - Rotate tokens periodically (contact FryNetworks for rotation)
8. **Caching:** Cache successful lookups to reduce API calls

## Interactive Documentation & Role-Based Access

The interactive API documentation (`/docs`) is only available after authentication. As a FlxTime partner, your token grants access to FlxTime-specific endpoint.

- **FlxTime tokens:** Only FlxTime endpoint (e.g., `/credentials/{miner_key}/exists`)
- **Public (unauthenticated):** No endpoints or schemas are visible

**How to use:**
1. Go to [https://hardwareapi.frynetworks.com/docs](https://hardwareapi.frynetworks.com/docs)
2. Click the "Authorize" button (🔒 lock icon) at the top
3. Enter your bearer token (just the token, no "Bearer" prefix)
4. Click "Authorize" and then "Close"
5. The docs will instantly update to show only FlxTime endpoint
6. Navigate to `/credentials/{miner_key}/exists` and use "Try it out" as needed

**Note:** If you log out or change tokens, the docs will automatically update and redirect as needed.

## Support

For technical support, API issues, or token rotation requests:

- **Email:** dude350z@frynetworks.com / helpdesk@frynetworks.com

## Security & Rate Limiting

- **Scanner Mitigation:** Excessive 404s (e.g., probing) trigger permanent auto-ban for the offending IP. Bans are persistent and managed by admins.
- **Rate Limiting:** 100 requests/minute per IP. See headers and error handling above.

**Last Updated:** October 25, 2025  
**API Version:** 2.1.0
