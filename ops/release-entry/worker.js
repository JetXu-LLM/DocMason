function jsonResponse(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function nonemptyString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

function currentTimestamp() {
  return new Date().toISOString();
}

function currentEventDay() {
  return currentTimestamp().slice(0, 10);
}

function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string") {
    return false;
  }
  let mismatch = left.length ^ right.length;
  const limit = Math.max(left.length, right.length);
  for (let index = 0; index < limit; index += 1) {
    const leftCode = index < left.length ? left.charCodeAt(index) : 0;
    const rightCode = index < right.length ? right.charCodeAt(index) : 0;
    mismatch |= leftCode ^ rightCode;
  }
  return mismatch === 0;
}

async function handleAdminPublish(request, env) {
  const payload = await readJson(request);
  const releaseVersion = nonemptyString(payload?.release_version);
  const releaseUrl = nonemptyString(payload?.release_url);
  const publishedAt = nonemptyString(payload?.published_at) || currentTimestamp();
  const channels = Array.isArray(payload?.channels) ? payload.channels : [];
  if (!releaseVersion || !releaseUrl || channels.length === 0) {
    return jsonResponse(400, {
      error: "release_version, release_url, and channels are required.",
    });
  }

  const updatedAt = currentTimestamp();
  for (const channel of channels) {
    const distributionChannel = nonemptyString(channel?.distribution_channel);
    const assetName = nonemptyString(channel?.asset_name);
    const assetUrl = nonemptyString(channel?.asset_url);
    if (!distributionChannel || !assetName || !assetUrl) {
      return jsonResponse(400, {
        error: "Each channel requires distribution_channel, asset_name, and asset_url.",
      });
    }
    await env.DB.prepare(
      `
      INSERT INTO release_current (
        distribution_channel,
        latest_version,
        published_at,
        release_url,
        asset_url,
        asset_name,
        updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(distribution_channel) DO UPDATE SET
        latest_version = excluded.latest_version,
        published_at = excluded.published_at,
        release_url = excluded.release_url,
        asset_url = excluded.asset_url,
        asset_name = excluded.asset_name,
        updated_at = excluded.updated_at
      `
    )
      .bind(
        distributionChannel,
        releaseVersion,
        publishedAt,
        releaseUrl,
        assetUrl,
        assetName,
        updatedAt
      )
      .run();
  }

  return jsonResponse(200, {
    schema_version: 1,
    status: "ok",
    updated_channels: channels.length,
  });
}

async function handleCheck(request, env) {
  const payload = await readJson(request);
  const distributionChannel = nonemptyString(payload?.distribution_channel);
  const installationHash = nonemptyString(payload?.installation_hash);
  const trigger = nonemptyString(payload?.trigger);
  if (!distributionChannel || !installationHash || !trigger) {
    return jsonResponse(400, {
      error: "distribution_channel, installation_hash, and trigger are required.",
    });
  }

  await env.DB.prepare(
    `
    INSERT OR IGNORE INTO daily_activity (
      event_day,
      installation_hash,
      distribution_channel,
      recorded_at,
      trigger
    ) VALUES (?, ?, ?, ?, ?)
    `
  )
    .bind(
      currentEventDay(),
      installationHash,
      distributionChannel,
      currentTimestamp(),
      trigger
    )
    .run();

  const row = await env.DB.prepare(
    `
    SELECT distribution_channel, latest_version, published_at, release_url, asset_url, asset_name
    FROM release_current
    WHERE distribution_channel = ?
    `
  )
    .bind(distributionChannel)
    .first();
  if (!row) {
    return jsonResponse(404, {
      error: `No published release metadata exists for ${distributionChannel}.`,
    });
  }

  return jsonResponse(200, {
    schema_version: 1,
    current_release: {
      distribution_channel: row.distribution_channel,
      latest_version: row.latest_version,
      published_at: row.published_at,
      release_url: row.release_url,
      asset_url: row.asset_url,
      asset_name: row.asset_name,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/v1/check") {
      return handleCheck(request, env);
    }
    if (request.method === "POST" && url.pathname === "/v1/admin/release-current") {
      const authHeader = request.headers.get("authorization") || "";
      const token = nonemptyString(env.DOCMASON_RELEASE_ENTRY_ADMIN_TOKEN);
      if (!token || !constantTimeEqual(authHeader, `Bearer ${token}`)) {
        return jsonResponse(401, { error: "Unauthorized." });
      }
      return handleAdminPublish(request, env);
    }
    return jsonResponse(404, { error: "Not found." });
  },
};
