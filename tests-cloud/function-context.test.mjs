import test from 'node:test';
import assert from 'node:assert/strict';
import {runtimeEnv} from '../netlify/lib/deployment-context.mjs';
import {contextScope} from '../netlify/lib/security.mjs';

const read = values => name => values[name];
test('hosted Function deploy Context overrides missing or contradictory build environment metadata', () => {
  const build = {CONTEXT:'production',DEPLOY_ID:'production-build',COURSEMATCH_SESSION_SECRET:'session-fixture',COURSEMATCH_WORKER_TOKEN:'worker-fixture'};
  const context = {deploy:{context:'deploy-preview',id:'actual-preview'}};
  const resolved = runtimeEnv(context,read(build));
  assert.equal(contextScope(resolved),'preview-actual-preview');
  assert.equal(resolved.COURSEMATCH_SESSION_SECRET,build.COURSEMATCH_SESSION_SECRET);
  assert.equal(resolved.COURSEMATCH_WORKER_TOKEN,build.COURSEMATCH_WORKER_TOKEN);
  assert.equal(build.CONTEXT,'production');
  assert.equal(build.DEPLOY_ID,'production-build');
  assert.equal(contextScope(runtimeEnv({deploy:{context:'production',id:'production'}},read({}))), 'production');
});
test('distinct real preview deploy IDs remain isolated when runtime build variables are absent', () => {
  const a = runtimeEnv({deploy:{context:'deploy-preview',id:'preview-a'}},read({}));
  const b = runtimeEnv({deploy:{context:'deploy-preview',id:'preview-b'}},read({}));
  assert.notEqual(contextScope(a),contextScope(b));
  assert.notEqual(contextScope(a),'production');
});
test('local tests and Netlify Dev retain explicit environment fallback without mutations', () => {
  assert.equal(contextScope(runtimeEnv(undefined,read({}))), 'local-local');
  assert.equal(contextScope(runtimeEnv({},read({CONTEXT:'dev'}))), 'local-dev');
  assert.equal(contextScope(runtimeEnv({},read({CONTEXT:'deploy-preview',DEPLOY_ID:'local-preview'}))), 'preview-local-preview');
});
test('branch deploy without BRANCH uses actual deployment identity instead of a shared unavailable scope', () => {
  const branch = runtimeEnv({deploy:{context:'branch-deploy',id:'branch-deploy-123'}},read({BRANCH:'unreliable-runtime-branch'}));
  assert.equal(contextScope(branch),'branch-branch-deploy-123');
});
test('missing hosted preview isolation metadata fails closed', () => {
  assert.throws(() => runtimeEnv({deploy:{context:'deploy-preview'}},read({})),{name:'MissingDeployScopeError'});
  assert.throws(() => runtimeEnv({deploy:{context:'branch-deploy'}},read({})),{name:'MissingDeployScopeError'});
});
