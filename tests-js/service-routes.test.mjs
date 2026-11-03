import test from 'node:test';
import assert from 'node:assert/strict';
import {validatedActiveRoutes} from '../scripts/service_routes.mjs';

const routes=origin=>`/api/worker/* /404.html 404!\n/api/* ${origin}/api/:splat 200\n/pdf/* ${origin}/pdf/:splat 200\n/healthz ${origin}/healthz 200\n`;
test('static rebuild preserves the configured named tunnel and the quick-tunnel fallback',()=>{
  const named=routes('https://coursematch-api.example.com');
  assert.equal(validatedActiveRoutes(named,'coursematch-api.example.com'),named);
  const quick=routes('https://some-live-tunnel.trycloudflare.com');
  assert.equal(validatedActiveRoutes(quick),quick);
  assert.equal(validatedActiveRoutes(quick,'coursematch-api.example.com'),quick);
});
test('static rebuild rejects an unconfigured origin, route injection and mismatched upstreams',()=>{
  const named=routes('https://coursematch-api.example.com');
  for(const [value,host] of [[named,undefined],[named,'different.example.com'],
      [named+'/* /index.html 200\n','coursematch-api.example.com'],[named+'\n','coursematch-api.example.com'],
      [named.replace('/404.html 404!','https://bad.example.com 200'),'coursematch-api.example.com'],
      [named.replace('/pdf/* https://coursematch-api.example.com','/pdf/* https://bad.example.com'),'coursematch-api.example.com'],
      [routes('http://coursematch-api.example.com'),'coursematch-api.example.com'],
      [routes('https://-bad.trycloudflare.com'),undefined]]) {
    assert.throws(()=>validatedActiveRoutes(value,host));
  }
});
test('configured hostname validation matches supervisor DNS restrictions',()=>{
  for(const host of ['',null,12,'localhost','UPPER.example.com','bad.example.com/path','bad.example.com\n','-bad.example.com','bad..example.com','foo.123']) {
    assert.throws(()=>validatedActiveRoutes(routes('https://good.example.com'),host),/Invalid configured service hostname/);
  }
});
