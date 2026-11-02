export function runtimeEnv(context = {}, readEnv = name => globalThis.Netlify?.env?.get(name) ?? process.env[name]) {
  // Deploy metadata comes from the actual request Context in hosted Functions.
  // Environment fallbacks support local tests/Netlify Dev without that Context;
  // CONTEXT and DEPLOY_ID are build variables, not guaranteed runtime variables.
  const env = Object.fromEntries(['CONTEXT','DEPLOY_ID','BRANCH','COURSEMATCH_SESSION_SECRET','COURSEMATCH_WORKER_TOKEN'].map(name => [name,readEnv(name)]));
  const deploy = context?.deploy;
  if (typeof deploy?.context === 'string' && deploy.context) env.CONTEXT = deploy.context;
  if (typeof deploy?.id === 'string' && deploy.id) env.DEPLOY_ID = deploy.id;
  if (env.CONTEXT === 'branch-deploy' && typeof deploy?.id === 'string' && deploy.id) env.BRANCH = deploy.id;
  else if (env.CONTEXT === 'branch-deploy' && !env.BRANCH && env.DEPLOY_ID) env.BRANCH = env.DEPLOY_ID;
  if ((env.CONTEXT === 'deploy-preview' && !env.DEPLOY_ID) || (env.CONTEXT === 'branch-deploy' && !env.BRANCH)) {
    const error = new Error('Deploy isolation metadata is unavailable');
    error.name = 'MissingDeployScopeError';
    throw error;
  }
  return env;
}
