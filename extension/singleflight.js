export function singleFlight(operation) {
  let running = null;

  return (...args) => {
    if (running === null) {
      running = Promise.resolve()
        .then(() => operation(...args))
        .finally(() => { running = null; });
    }
    return running;
  };
}
