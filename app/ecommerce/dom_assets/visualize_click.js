(args) => {
    const { x, y } = args;
    console.log(`CARTMIND: Visualizing click at ${x}, ${y}`);

    // Create the circle element
    const circle = document.createElement('div');
    circle.id = 'cartmind-click-visualizer-' + Date.now();

    // Style the circle - MORE PROMINENT
    Object.assign(circle.style, {
        position: 'fixed',
        left: (x - 30) + 'px', // Centering for 60px width
        top: (y - 30) + 'px',
        width: '60px',
        height: '60px',
        borderRadius: '50%',
        backgroundColor: 'rgba(0, 80, 255, 0.6)', // Deeper, more opaque blue
        border: '4px solid rgba(0, 255, 255, 0.9)', // Cyan border for contrast
        boxShadow: '0 0 15px rgba(0, 100, 255, 0.8)', // Add glow
        pointerEvents: 'none',
        zIndex: '2147483647',
        transform: 'scale(0.2)',
        transition: 'transform 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275), opacity 0.4s ease-out',
        opacity: '1'
    });

    document.body.appendChild(circle);

    // Animate and remove
    requestAnimationFrame(() => {
        circle.style.transform = 'scale(1.8)'; // Larger expansion
        circle.style.opacity = '0';

        setTimeout(() => {
            if (circle.parentNode) {
                circle.parentNode.removeChild(circle);
            }
        }, 400);
    });

    return { success: true, x, y };
}
