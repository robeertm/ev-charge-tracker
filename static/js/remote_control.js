/* Remote vehicle control — one implementation for every page that offers it.
 *
 * Written with EVENT DELEGATION on document, not with handlers bound to
 * each element at load time. That is a deliberate choice, not a style
 * preference: the first version bound handlers from an inline script that
 * sat in the middle of settings.html and therefore ran BEFORE the section
 * it was binding to had been rendered. querySelectorAll matched nothing,
 * no handler was attached, and clicking the switch produced no request,
 * no error and no clue — it just flipped back on the next page load.
 *
 * Delegation cannot fail that way: the listener lives on document, which
 * always exists, and the lookup happens when the click happens.
 *
 * Labels and the confirmation text come from data attributes, so this
 * file needs no translations of its own.
 */
(function () {
    'use strict';

    function box(el) { return el.closest('[data-remote-vehicle]'); }

    function say(b, html) {
        var s = b.querySelector('.rc-status');
        if (s) s.innerHTML = html;
    }

    function setControls(b, enabled) {
        b.querySelectorAll('.rc-cmd, .rc-limit').forEach(function (el) {
            el.disabled = !enabled;
        });
    }

    document.addEventListener('change', function (ev) {
        var el = ev.target;
        if (!el.classList || !el.classList.contains('rc-optin')) return;
        var b = box(el);
        if (!b) return;
        var want = el.checked;
        el.disabled = true;
        fetch('/api/vehicle/' + b.dataset.remoteVehicle + '/remote-optin', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: want})
        }).then(function (r) { return r.json(); }).then(function (d) {
            // Unlock only once the SERVER says so. Showing a permission
            // the backend does not have is how a button ends up looking
            // ready while doing nothing.
            var real = !!(d && d.ok && d.enabled);
            el.checked = real;
            setControls(b, real);
            say(b, '');
            document.querySelectorAll(
                '[data-remote-vehicle="' + b.dataset.remoteVehicle + '"]'
            ).forEach(function (other) {
                if (other === b) return;
                var o = other.querySelector('.rc-optin');
                if (o) o.checked = real;
                setControls(other, real);
            });
        }).catch(function (e) {
            el.checked = !want;
            say(b, '<span class="text-danger">' + e.message + '</span>');
        }).then(function () { el.disabled = false; });
    });

    document.addEventListener('click', function (ev) {
        var el = ev.target.closest ? ev.target.closest('.rc-cmd') : null;
        if (!el) return;
        var b = box(el);
        if (!b) return;
        var cmd = el.dataset.cmd;
        var body = {};

        if ((b.dataset.rcSensitive || '').split(',').indexOf(cmd) !== -1) {
            if (!window.confirm(b.dataset.rcConfirm || cmd)) return;
            body.confirm = '1';
        }
        var lim = b.querySelector('.rc-limit');
        if (cmd === 'charging_limit' && lim) body.target_soc = lim.value;

        el.disabled = true;
        say(b, '<span class="spinner-border spinner-border-sm"></span>');
        fetch('/api/vehicle/' + b.dataset.remoteVehicle + '/remote/' + cmd, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        }).then(function (r) { return r.json(); }).then(function (d) {
            // "sent", never "done": these APIs answer 202 and the car
            // acts afterwards. Claiming success would outrun the facts.
            say(b, d && d.ok
                ? '<span class="text-success">' + (d.message || '') + '</span>'
                : '<span class="text-danger">' + ((d && d.error) || '') + '</span>');
        }).catch(function (e) {
            say(b, '<span class="text-danger">' + e.message + '</span>');
        }).then(function () { el.disabled = false; });
    });
})();
