/* global Chart:false */

$(function () {
  'use strict'

  /* ChartJS
   * -------
   * Here we will create a few charts using ChartJS
   */

  //-----------------------
  // - MONTHLY SALES CHART -
  //-----------------------

  // Get context with jQuery - using jQuery's .get() method.
  var salesChartCanvas = $('#salesChart').get(0).getContext('2d')

  var salesChartData = {
    labels: ['January', 'February', 'March', 'April', 'May', 'June', 'July'],
    datasets: [
      {
        label: 'Digital Goods',
        backgroundColor: 'rgba(60,141,188,0.9)',
        borderColor: 'rgba(60,141,188,0.8)',
        pointRadius: false,
        pointColor: '#3b8bba',
        pointStrokeColor: 'rgba(60,141,188,1)',
        pointHighlightFill: '#fff',
        pointHighlightStroke: 'rgba(60,141,188,1)',
        data: [28, 48, 40, 19, 86, 27, 90]
      },
      {
        label: 'Electronics',
        backgroundColor: 'rgba(210, 214, 222, 1)',
        borderColor: 'rgba(210, 214, 222, 1)',
        pointRadius: false,
        pointColor: 'rgba(210, 214, 222, 1)',
        pointStrokeColor: '#c1c7d1',
        pointHighlightFill: '#fff',
        pointHighlightStroke: 'rgba(220,220,220,1)',
        data: [65, 59, 80, 81, 56, 55, 40]
      }
    ]
  }

  var salesChartOptions = {
    maintainAspectRatio: false,
    responsive: true,
    legend: {
      display: false
    },
    scales: {
      xAxes: [{
        gridLines: {
          display: false
        }
      }],
      yAxes: [{
        gridLines: {
          display: false
        }
      }]
    }
  }

  // This will get the first returned node in the jQuery collection.
  // eslint-disable-next-line no-unused-vars
  var salesChart = new Chart(salesChartCanvas, {
    type: 'line',
    data: salesChartData,
    options: salesChartOptions
  }
  )

  //---------------------------
  // - END MONTHLY SALES CHART -
  //---------------------------

  //-------------
  // - PIE CHART -
  //-------------
  // Get context with jQuery - using jQuery's .get() method.
  var pieChartCanvas = $('#pieChart').get(0).getContext('2d')
  var pieData = {
    labels: [
      'Offensive Security',
      'Intermediate Labs',
      'Advanced Labs',
      'Introductory Labs',
      'Defensive Security',
      'Networking'
    ],
    datasets: [
      {
        data: [700, 500, 400, 600, 300, 100],
        backgroundColor: ['#f56954', '#00a65a', '#f39c12', '#00c0ef', '#3c8dbc', '#d2d6de']
      }
    ]
  }
  var pieOptions = {
    legend: {
      display: false
    }
  }
  // Create pie or douhnut chart
  // You can switch between pie and douhnut using the method below.
  // eslint-disable-next-line no-unused-vars
  var pieChart = new Chart(pieChartCanvas, {
    type: 'doughnut',
    data: pieData,
    options: pieOptions
  })

  //-----------------
  // - END PIE CHART -
  //-----------------

  /* Leaflet Map
   * ------------
   * Create a world map with markers using parametrized config
   */
  
  var mapElement = document.getElementById('map');
  if (mapElement && mapElement.dataset.centerLat) {
    var centerLat = parseFloat(mapElement.dataset.centerLat);
    var centerLng = parseFloat(mapElement.dataset.centerLng);
    var zoomLevel = parseInt(mapElement.dataset.zoom);
    
    var map = L.map('map').setView([centerLat, centerLng], zoomLevel);
    
    var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);
    
    try {
      var mapPoints = window.mapConfig ? window.mapConfig.points : [];
      console.log('Map points from global config:', mapPoints);
      
      if (Array.isArray(mapPoints) && mapPoints.length > 0) {
        mapPoints.forEach(function(point) {
          if (point.lat && point.lng) {
            var marker = L.marker([point.lat, point.lng]).addTo(map);
            
            var iconClass = 'fas fa-map-marker-alt';
            var iconColor = 'blue';
            
            switch(point.type) {
              case 'university':
                iconClass = 'fas fa-university';
                iconColor = 'green';
                break;
              case 'institute':
                iconClass = 'fas fa-school';
                iconColor = 'orange';
                break;
              case 'datacenter':
                iconClass = 'fas fa-server';
                iconColor = 'red';
                break;
            }
            
            marker.bindPopup(
              '<div>' +
                '<h6><i class="' + iconClass + '" style="color: ' + iconColor + '"></i> ' + point.name + '</h6>' +
                '<p>' + point.description + '</p>' +
              '</div>'
            );
          }
        });
      }
    } catch (error) {
      console.error('Error loading map points:', error);
    }
    
    if (typeof statesData !== 'undefined') {
      L.geoJson(statesData).addTo(map);
    }
    
    var updateMap = function() {
      var request = $.ajax({
        url: "/api/nodes",
        type: "GET",
        dataType: "json",
        contentType: "application/json",
      });
      request.done(function(data) {
        $.each(data.result, function(name, attrs) {
          L.marker([attrs.latitude, attrs.longitude]).addTo(map).bindPopup(attrs.tooltip);
        });
      });
    };
    setTimeout(updateMap, 50);
    
  } else {
    var map = L.map('map').setView([-15.13, -53.19], 4);
    var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);

    if (typeof statesData !== 'undefined') {
      L.geoJson(statesData).addTo(map);
    }
    
    var updateMap = function() {
      var request = $.ajax({
        url: "/api/nodes",
        type: "GET",
        dataType: "json",
        contentType: "application/json",
      });
      request.done(function(data) {
        $.each(data.result, function(name, attrs) {
          L.marker([attrs.latitude, attrs.longitude]).addTo(map).bindPopup(attrs.tooltip);
        });
      });
    };
    setTimeout(updateMap, 50);
  }
})
