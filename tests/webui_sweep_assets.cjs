const fs = require('node:fs');
const path = require('node:path');

// Load the same dependency before each panel as the deferred platform scripts.
module.exports = function panelCode(panelPath) {
  return [path.join(path.dirname(panelPath), 'sweep-settings.js'), panelPath]
    .map(file => fs.readFileSync(file, 'utf8')).join('\n');
};
