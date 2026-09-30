document.querySelectorAll('input[data-password-toggle]').forEach(input => {
    const group = document.createElement('div');
    group.className = 'input-group';
    input.parentNode.insertBefore(group, input);
    group.appendChild(input);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-outline-secondary';
    button.setAttribute('aria-label', 'Show password');
    button.setAttribute('aria-pressed', 'false');
    if (input.id) button.setAttribute('aria-controls', input.id);
    const icon = document.createElement('i');
    icon.className = 'bi bi-eye';
    icon.setAttribute('aria-hidden', 'true');
    button.appendChild(icon);
    group.appendChild(button);

    button.addEventListener('click', () => {
        const visible = input.type === 'password';
        input.type = visible ? 'text' : 'password';
        button.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
        button.setAttribute('aria-pressed', String(visible));
        icon.className = visible ? 'bi bi-eye-slash' : 'bi bi-eye';
    });
});
