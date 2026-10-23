// Filters and groups the Branches panel rows by remote. Works as a classic
// browser script (window.ArgonautaBranches) and under Node's require.
(function (root) {
  "use strict";

  // Returns the locals and remote groups whose full branch name matches the filter.
  function groupBranches(branches, remotes, filter) {
    const query = String(filter == null ? "" : filter).trim().toLowerCase();
    const matches = (name) => name.toLowerCase().includes(query);
    const all = branches || [];
    const locals = [];
    const byRemote = new Map();
    let localTotal = 0, remoteTotal = 0;
    for (const br of all) {
      if (br.remote == null) {
        localTotal++;
        if (matches(br.name)) locals.push(br);
        continue;
      }
      remoteTotal++;
      if (!matches(br.name)) continue;
      if (!byRemote.has(br.remote)) byRemote.set(br.remote, []);
      const prefix = br.remote + "/";
      const short = br.name.startsWith(prefix) ? br.name.slice(prefix.length) : br.name;
      byRemote.get(br.remote).push({ ...br, short });
    }
    // remote order: as listed in `remotes`, then any others alphabetically
    const known = (remotes || []).map((r) => r.name);
    const unknown = [...byRemote.keys()].filter((n) => !known.includes(n)).sort();
    const order = [...known, ...unknown].filter((n) => byRemote.has(n));
    const groups = order.map((remote) => ({ remote, branches: byRemote.get(remote) }));
    const remoteShown = groups.reduce((n, g) => n + g.branches.length, 0);
    return {
      locals,
      groups,
      localShown: locals.length,
      localTotal,
      remoteShown,
      remoteTotal,
    };
  }

  const api = { groupBranches };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ArgonautaBranches = api;
})(typeof window !== "undefined" ? window : globalThis);
