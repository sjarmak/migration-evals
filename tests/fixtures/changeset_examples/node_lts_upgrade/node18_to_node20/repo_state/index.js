'use strict';

const url = require('url');

function getHost(input) {
  return url.parse(input).host;
}

module.exports = { getHost };
