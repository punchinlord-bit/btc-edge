'use strict';
const express = require('express');

// Mount: app.use('/api/btc-15m', createBetRoutes(betFeed));
// PROTECT POST /bets with your existing auth (or remove it) if bots call recordBet() in-process.
function createBetRoutes(feed) {
  const router = express.Router();
  router.use(express.json({ limit: '256kb' }));
  router.get('/bets', (_req, res) => res.json(feed.current()));
  router.post('/bets', (req, res) => {
    try { res.json(feed.recordBet(req.body && req.body.venue, req.body)); }
    catch (e) { res.status(400).json({ error: e.message }); }
  });
  return router;
}
module.exports = { createBetRoutes };
