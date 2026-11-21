// Simple async delay function
const delay = (ms) => new Promise((res) => setTimeout(res, ms));
module.exports = { delay };
