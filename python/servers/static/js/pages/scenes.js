window.addEventListener('DOMContentLoaded', function() {
    loadScenes();
});

function loadScenes() {
    fetch('/api/scenes/all')
        .then(response => response.json())
        .then(data => {
            document.getElementById('loading-state').classList.add('hidden');
            
            if (data.success && data.scenes && data.scenes.length > 0) {
                displayScenes(data.scenes);
            } else if (data.needs_config) {
                showError('Hue Bridge not configured');
            } else {
                showError('No scenes found');
            } })
        .catch(err => {
            console.error('Error loading scenes:', err);
            document.getElementById('loading-state').classList.add('hidden');
            showError('Connection error - Please check your bridge');
        });
}

function displayScenes(scenes) {
    const container = document.getElementById('scenes-list');
    container.innerHTML = '';
    
    // Group scenes by room
    const scenesByRoom = {};
    scenes.forEach(scene => {
        const roomName = scene.room_name || 'Unknown Room';
        if (!scenesByRoom[roomName]) {
            scenesByRoom[roomName] = [];
        }
        scenesByRoom[roomName].push(scene);
    });
    
    // Sort room names
    const sortedRoomNames = Object.keys(scenesByRoom).sort();
    
    // Create sections for each room
    sortedRoomNames.forEach(roomName => {
        const roomSection = document.createElement('div');
        roomSection.className = 'col-span-full mb-6';
        
        const roomScenes = scenesByRoom[roomName];
        const roomHeader = document.createElement('div');
        roomHeader.className = 'flex items-center mb-3';
        roomHeader.innerHTML = `
            <h2 class="text-base font-semibold text-gray-900 mr-2 flex items-center">
                <i class="fas fa-door-open text-pink-500 mr-2 text-sm"></i><span class="truncate">${roomName}</span>
            </h2>
            <span class="px-2 py-0.5 text-[11px] font-medium rounded-full bg-pink-50 text-pink-700">
                ${roomScenes.length} scene${roomScenes.length !== 1 ? 's' : ''}
            </span>
        `;
        roomSection.appendChild(roomHeader);
        
        const scenesGrid = document.createElement('div');
        scenesGrid.className = 'grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3';
        
        roomScenes.forEach(scene => {
            const sceneCard = document.createElement('div');
            sceneCard.className = 'bg-white rounded-lg p-3 shadow-sm hover:shadow-md transition-all duration-200 border border-transparent hover:border-pink-200 cursor-pointer flex flex-col items-stretch';
            sceneCard.setAttribute('data-scene-id', scene.id);
            
            sceneCard.innerHTML = `
                <div class="flex items-center gap-3 mb-4">
                    <div class="w-9 h-9 rounded-md bg-gradient-to-br from-pink-400 to-pink-600 flex items-center justify-center text-white text-sm">
                        <i class="fas fa-palette text-sm"></i>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="text-sm font-medium text-gray-900 truncate">${scene.name}</div>
                    </div>
                </div>
                <div class="mt-auto flex items-center text-center justify-start gap-3">
                    <button onclick="activateScene('${scene.id}', '${scene.name.replace(/'/g, "\\'")}', event)" class="w-full flex  justify-center px-3 py-1.5 bg-pink-600 text-white text-xs rounded-md hover:bg-pink-700 transition-colors inline-flex items-center">
                        <i class="fas fa-play mr-1 text-xs"></i>Activate
                    </button>
                </div>
            `;
            
            scenesGrid.appendChild(sceneCard);
        });
        
        roomSection.appendChild(scenesGrid);
        container.appendChild(roomSection);
    });
    
    document.getElementById('scenes-container').classList.remove('hidden');
}

function activateScene(sceneId, sceneName, event) {
    event.stopPropagation();
    
    const btn = event.target.closest('button');
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Activating...';
    
    fetch(`/api/scenes/${sceneId}/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            btn.innerHTML = '<i class="fas fa-check mr-2"></i>Activated!';
            btn.classList.remove('bg-pink-600', 'hover:bg-pink-700');
            btn.classList.add('bg-green-600');
            
            setTimeout(() => {
                btn.innerHTML = originalContent;
                btn.classList.remove('bg-green-600');
                btn.classList.add('bg-pink-600', 'hover:bg-pink-700');
                btn.disabled = false;
            }, 2000);
        } else {
            btn.innerHTML = originalContent;
            btn.disabled = false;
            showToast('Error', 'Failed to activate scene: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(err => {
        console.error('Error activating scene:', err);
        btn.innerHTML = originalContent;
        btn.disabled = false;
        showToast('Error', 'Failed to activate scene', 'error');
    });
}

function showError(message) {
    document.getElementById('error-message').textContent = message;
    document.getElementById('error-state').classList.remove('hidden');
}
