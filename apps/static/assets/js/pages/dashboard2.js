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

  $.get('/api/lab/usage_stats', function(data) {
    console.log('Dashboard stats data:', data);

    const salesChartCanvas = $('#salesChart').get(0).getContext('2d')
    const salesChartData = {
      labels: data.months,
      datasets: [
        {
          label: 'Lab Instances',
          backgroundColor: 'rgba(60,141,188,0.9)',
          borderColor: 'rgba(60,141,188,0.8)',
          pointRadius: false,
          pointColor: '#3b8bba',
          pointStrokeColor: 'rgba(60,141,188,1)',
          pointHighlightFill: '#fff',
          pointHighlightStroke: 'rgba(60,141,188,1)',
          data: data.labs_executed_counts
        }
      ]
    }

    const salesChartOptions = {
      maintainAspectRatio: true,
      responsive: true,
      legend: {
        display: true
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
      },
      tooltips: {
        mode: 'index',
        intersect: false
      },
      hover: {
        mode: 'index',
        intersect: false
      }
    }

    new Chart(salesChartCanvas, {
      type: 'line',
      data: salesChartData,
      options: salesChartOptions
    })

    $('#current-month-completed-labs').text(data.completed_labs_from_current_month)
    $('#current-month-answers-questions').text(data.answered_questions_from_current_month)
    $('#current-month-completed-challenges').text(data.completed_challenges_from_current_month)

    const labInstancesCreatedInterval = document.getElementById('lab-instances-created-interval');
    labInstancesCreatedInterval.innerHTML = `<strong>Lab instances created: ${data.start_date} - ${data.end_date}</strong>`;

    const usageStatisticsInterval = document.getElementById('usage-statistics-interval');
    usageStatisticsInterval.innerHTML = `<strong>Data interval: ${data.start_date} - ${data.end_date}</strong>`;

    const usageStatisticsCanvas = $('#usage-statistics-chart').get(0).getContext('2d')
    const usageStatisticsData = {
      labels: ['Completed labs', 'Completed challenges', 'Answered questions'],
      datasets: [{
        data: [
          data.completed_labs_from_last_six_months,
          data.completed_challenges_from_last_six_months,
          data.answered_questions_from_last_six_months
        ],
        backgroundColor: [
          '#a963ffff',
          '#bdcc8aff',
          '#7acfcfff',
        ],
        borderColor: '#000',
      }]
    };
    const usageStatisticsConfig = {
        scales: {
          xAxes: [{
            ticks: {
              beginAtZero: true
            },
            gridLines: {
              display: false
            }
          }],
          yAxes: [{
            gridLines: {
              display: false
            }
          }]
        },
        maintainAspectRatio: true,
        responsive: true,
        legend: {
          display: false
        },
        tooltips: {
          mode: 'index',
          intersect: false
        },
        hover: {
          mode: 'index',
          intersect: false
        }
    };
    new Chart(usageStatisticsCanvas, {
      type: 'horizontalBar',
      data: usageStatisticsData,
      options: usageStatisticsConfig
    })

    updateGrowthPercentage('completed-labs-growth', data.completed_labs_growth);
    updateGrowthPercentage('completed-challenges-growth', data.completed_challenges_growth);
    updateGrowthPercentage('answered-questions-growth', data.answered_questions_growth);
  })

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

  const updateGrowthPercentage = (elementId, growthValue) => {
    const element = document.getElementById(elementId);
    if (growthValue > 0) {
      element.innerHTML = `<span class="description-percentage text-success"><i class="fas fa-caret-up"></i> ${growthValue}%</span>`;
    } else if (growthValue < 0) {
      element.innerHTML = `<span class="description-percentage text-danger"><i class="fas fa-caret-down"></i> ${Math.abs(growthValue)}%</span>`;
    }
  };
})
