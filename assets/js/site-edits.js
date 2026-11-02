(function (root) {
    const clone = value => JSON.parse(JSON.stringify(value));
    const text = value => String(value ?? '').normalize('NFC').trim().replace(/\s+/g, ' ');
    const scopeOf = program => ({ program: program.name, degree: program.degree, plan_type: program.plan_type, version: String(program.version) });
    const matches = (scope, context) => ['program', 'degree', 'plan_type', 'version'].every(key => !text(scope[key]) || text(scope[key]) === text(context[key]));
    const identityMatches = (course, source) => ['code', 'name', 'hours'].every(key => text(course[key]) === text(source[key]));
    function apply(data, registry) {
        data.programs.forEach(program => program.courses.forEach((course, index) => { course._sourceCourseIndex = index; }));
        for (const entry of registry.entries || []) {
            const program = data.programs.find(program => String(program.id) === String(entry.program_id));
            const course = program?.courses[entry.course_index];
            if (!course || !identityMatches(course, entry.source)) continue;
            const context = scopeOf(program);
            const original = data.course_details[course.code];
            const variants = original ? (original.variants || [original]) : [];
            const eligible = variants.filter(variant => text(variant.title) === text(course.name));
            const scopes = variant => variant.scopes || (variant.scope ? [variant.scope] : []);
            const baseline = eligible.find(variant => scopes(variant).some(scope => matches(scope, context)))
                || eligible.find(variant => !scopes(variant).length);
            const details = baseline ? clone(baseline) : {};
            for (const variant of eligible) {
                variant.excluded_scopes = [...(variant.excluded_scopes || []), context];
            }
            Object.assign(course, entry.fields, { _siteEditRevision: entry.revision });
            details.title = course.name;
            details.scopes = [context];
            delete details.scope;
            delete details.excluded_scopes;
            details.attachments = (entry.files || []).filter(file => file.path !== entry.active_pdf_path)
                .map(file => ({ pdf_url: file.path, label: file.file_name }));
            if (entry.active_pdf_path) {
                if (details.pdf_url && details.pdf_url !== entry.active_pdf_path) {
                    details.attachments.unshift({ pdf_url: details.pdf_url, label: 'التوصيف السابق' });
                }
                details.pdf_url = entry.active_pdf_path;
                details.summary = '';
                details.match_status = 'user_published';
                details.specification_warning = '';
            }
            if (details.pdf_url || details.attachments.length) {
                const target = data.course_details[course.code];
                if (!target) data.course_details[course.code] = { variants: [details] };
                else {
                    if (!target.variants) data.course_details[course.code] = { variants: [clone(target)] };
                    data.course_details[course.code].variants.unshift(details);
                }
            }
        }
        return data;
    }
    const api = { apply, identityMatches, matches, scopeOf };
    if (typeof module !== 'undefined') module.exports = api;
    else root.CourseSiteEdits = api;
})(typeof window !== 'undefined' ? window : globalThis);
