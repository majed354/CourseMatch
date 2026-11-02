/** Static UI projection only. The canonical catalog and API remain complete.
 * One shared schema drives this builder and the Python publication guard.
 * excluded_rows retains array length only: app.js does not read row contents.
 */
import fs from 'node:fs';
const schema=JSON.parse(fs.readFileSync(new URL('./public_catalog_schema.json',import.meta.url),'utf8'));
function project(value, rule) {
  if(Array.isArray(rule)) {
    if(!Array.isArray(value))throw Error('invalid_public_catalog_array');
    return value.map(item=>project(item,rule[0]));
  }
  if(typeof rule==='object') {
    if(value===null&&rule.nullable)return null;
    return project(value,rule.ref);
  }
  if(rule==='scalar') {
    if(value!==null&&(!['string','number','boolean'].includes(typeof value)||(typeof value==='number'&&!Number.isFinite(value))))throw Error('invalid_public_catalog_scalar');
    return value;
  }
  if(value===null||typeof value!=='object'||Array.isArray(value))throw Error('invalid_public_catalog_object');
  const fields=schema.definitions[rule];
  if(!fields)throw Error('unknown_public_catalog_schema_rule');
  return Object.fromEntries(Object.entries(fields).filter(([key])=>Object.hasOwn(value,key)).map(([key,child])=>[key,project(value[key],child)]));
}
export function projectPublicCatalog(catalog) {return project(catalog,schema.root);}
