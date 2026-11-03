// Keep rebuilds restricted to the supervisor's exact four public routes.
export function validatedActiveRoutes(routes, namedHostname) {
  const label='[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?';
  if (namedHostname !== undefined && (typeof namedHostname !== 'string' || namedHostname.length > 253 || /\s/.test(namedHostname)
      || !new RegExp(`^${label}(?:\\.${label})+$`).test(namedHostname) || !/\.[a-z][a-z0-9-]*$/.test(namedHostname))) {
    throw Error('Invalid configured service hostname');
  }
  const match=routes.match(/^\/api\/worker\/\* \/404\.html 404!\n\/api\/\* (https:\/\/[^\s/]+)\/api\/:splat 200\n\/pdf\/\* \1\/pdf\/:splat 200\n\/healthz \1\/healthz 200\n$/);
  const origin=match?.[1];
  if (!match || match[0] !== routes || (!new RegExp(`^https:\\/\\/${label}\\.trycloudflare\\.com$`).test(origin)
      && origin !== (namedHostname === undefined ? undefined : `https://${namedHostname}`))) {
    throw Error('Invalid active service routes');
  }
  return routes;
}
